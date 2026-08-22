"""One-shot headless runner: `claude -p`, `codex exec`, `grok -p`, `cursor-agent -p`.

Simple and dependency-free. No streaming from the CLI itself for plain-text
mode, but stream-json output is forwarded chunk by chunk.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any, AsyncIterator

from ..config import AgentSpec
from .base import BaseRunner, Event, OnEvent


_TERMINATE_TIMEOUT = 1.0
_STDERR_CAPTURE_LIMIT = 64 * 1024
_PIPE_READ_SIZE = 64 * 1024


def _remaining(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _consume_future(future: asyncio.Future[Any]) -> None:
    with contextlib.suppress(BaseException):
        future.exception()


async def _drain_bounded_tail(
        reader: asyncio.StreamReader, limit: int = _STDERR_CAPTURE_LIMIT) -> bytes:
    """Continuously drain a pipe while retaining only its bounded tail."""
    tail = bytearray()
    while True:
        chunk = await reader.read(_PIPE_READ_SIZE)
        if not chunk:
            return bytes(tail)
        if len(chunk) >= limit:
            tail[:] = chunk[-limit:]
            continue
        overflow = len(tail) + len(chunk) - limit
        if overflow > 0:
            del tail[:overflow]
        tail.extend(chunk)


def _dig(obj: Any, dotted: str) -> Any:
    for part in filter(None, dotted.split(".")):
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def _text_from_json(payload: Any, spec: AgentSpec) -> str:
    if spec.exec_json_path:
        found = _dig(payload, spec.exec_json_path)
        if isinstance(found, str):
            return found
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("result", "text", "content", "message", "response", "output"):
            val = payload.get(key)
            if isinstance(val, str):
                return val
            if isinstance(val, dict):
                nested = _text_from_json(val, spec)
                if nested:
                    return nested
            if isinstance(val, list):
                parts = [p.get("text", "") for p in val if isinstance(p, dict)]
                if any(parts):
                    return "".join(parts)
    return ""


def _error_from_json(payload: Any) -> str:
    """Extract errors that some CLIs report in successful JSON processes."""
    if not isinstance(payload, dict):
        return ""
    explicit = payload.get("type") == "error" or payload.get("is_error") is True
    error = payload.get("error")
    if not explicit and not error:
        return ""

    for value in (error, payload.get("message"), payload.get("detail")):
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for key in ("message", "detail", "type", "code"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return "CLI returned a structured error"


class ExecRunner(BaseRunner):
    def __init__(self, spec: AgentSpec) -> None:
        super().__init__(spec)
        self._proc: asyncio.subprocess.Process | None = None
        self._proc_lock = asyncio.Lock()
        self._closed = False
        self.last_meta: dict[str, Any] = {}

    async def start(self, workdir: str) -> None:
        async with self._proc_lock:
            await super().start(workdir)
            self._closed = False

    async def cancel(self) -> None:
        await self._stop_current()

    async def close(self) -> None:
        await self._stop_current(closing=True)

    async def _stop_current(self, closing: bool = False) -> None:
        async with self._proc_lock:
            if closing:
                self._closed = True
            proc = self._proc
            if proc is None:
                return
            try:
                await self._reap(proc)
            finally:
                if self._proc is proc:
                    self._proc = None

    async def _release(self, proc: asyncio.subprocess.Process) -> None:
        async with self._proc_lock:
            try:
                await self._reap(proc)
            finally:
                if self._proc is proc:
                    self._proc = None

    async def _reap(self, proc: asyncio.subprocess.Process) -> None:
        cleanup = asyncio.create_task(self._terminate_and_wait(proc))
        try:
            await asyncio.shield(cleanup)
        except BaseException:
            # Cleanup must outlive cancellation of the caller that owns the CLI.
            with contextlib.suppress(BaseException):
                await cleanup
            raise

    async def _terminate_and_wait(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            await proc.wait()
            return

        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        waiter = asyncio.create_task(proc.wait())
        try:
            await asyncio.wait_for(
                asyncio.shield(waiter), timeout=_TERMINATE_TIMEOUT)
        except asyncio.TimeoutError:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            done, _pending = await asyncio.wait(
                {waiter}, timeout=_TERMINATE_TIMEOUT)
            if waiter not in done:
                waiter.cancel()
                waiter.add_done_callback(_consume_future)

    async def _spawn(self, *argv: str, stdin, stdout, stderr, cwd: str,
                     env: dict[str, str]) -> asyncio.subprocess.Process:
        async with self._proc_lock:
            if self._closed:
                raise RuntimeError(f"{self.spec.label}: exec runner is closed")
            if self._proc is not None and self._proc.returncode is None:
                raise RuntimeError(f"{self.spec.label}: exec process is already running")

            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *argv, stdin=stdin, stdout=stdout, stderr=stderr, cwd=cwd, env=env))
            try:
                proc = await asyncio.shield(spawn)
            except BaseException:
                # Cancellation can arrive after the OS process exists but before
                # create_subprocess_exec returns it to us. Reap it if that race wins.
                with contextlib.suppress(BaseException):
                    proc = await spawn
                    await self._reap(proc)
                raise
            self._proc = proc
            return proc

    async def stream(self, prompt: str, on_event: OnEvent | None = None
                     ) -> AsyncIterator[Event]:
        spec = self.spec
        self.last_meta = {}
        deadline = asyncio.get_running_loop().time() + spec.timeout
        argv = list(spec.exec_command)
        stdin_data: bytes | None = None
        if spec.exec_input == "stdin":
            stdin_data = prompt.encode()
        else:
            argv.append(prompt)

        env = {**os.environ, **spec.env}
        proc = await self._spawn(
            *argv,
            stdin=(asyncio.subprocess.PIPE
                   if stdin_data else asyncio.subprocess.DEVNULL),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workdir,
            env=env,
        )

        stderr_task: asyncio.Task[bytes] | None = None
        stdout_cleanup: asyncio.Task[bytes] | None = None
        try:
            if spec.exec_output == "stream-json":
                assert proc.stderr is not None
                stderr_task = asyncio.create_task(
                    _drain_bounded_tail(proc.stderr))
                async for ev in self._read_stream_json(
                        proc, stdin_data, deadline, stderr_task):
                    yield ev
                return

            try:
                remaining = _remaining(deadline)
                out, err = await asyncio.wait_for(
                    proc.communicate(stdin_data), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError from None

            stdout = out.decode(errors="replace").strip()
            stderr = err.decode(errors="replace").strip()
            reported_error = False

            if spec.exec_output == "json":
                try:
                    payload = json.loads(stdout)
                except json.JSONDecodeError:
                    yield Event("text", stdout)
                else:
                    if isinstance(payload, dict):
                        self.last_meta = payload
                    structured_error = _error_from_json(payload)
                    if structured_error:
                        yield Event("error", structured_error)
                        reported_error = True
                    else:
                        yield Event("text", _text_from_json(payload, spec) or stdout)
            else:
                yield Event("text", stdout)

            if proc.returncode not in (0, None) and not reported_error:
                yield Event("error", stderr or f"exit code {proc.returncode}")
            yield Event("done")
        finally:
            if spec.exec_output == "stream-json" and proc.stdout is not None:
                # A timeout or malformed overlong NDJSON line can leave stdout
                # paused with unread bytes. Keep draining while terminate/kill
                # waits for the subprocess transport to reach EOF.
                stdout_cleanup = asyncio.create_task(
                    _drain_bounded_tail(proc.stdout))
            try:
                await self._release(proc)
            finally:
                for drain in (stdout_cleanup, stderr_task):
                    if drain is None:
                        continue
                    if not drain.done():
                        drain.cancel()
                    with contextlib.suppress(BaseException):
                        await drain

    async def _read_stream_json(self, proc: asyncio.subprocess.Process,
                                stdin_data: bytes | None, deadline: float,
                                stderr_task: asyncio.Task[bytes]
                                ) -> AsyncIterator[Event]:
        """Consume newline-delimited JSON events (Codex-style `exec --json`)."""
        assert proc.stdout is not None
        if stdin_data and proc.stdin:
            try:
                proc.stdin.write(stdin_data)
                try:
                    remaining = _remaining(deadline)
                    await asyncio.wait_for(
                        proc.stdin.drain(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise TimeoutError from None
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()
            try:
                remaining = _remaining(deadline)
                await asyncio.wait_for(
                    proc.stdin.wait_closed(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError from None
            except (BrokenPipeError, ConnectionResetError):
                pass

        while True:
            try:
                remaining = _remaining(deadline)
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError from None
            if not line:
                break
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                text = line.decode(errors="replace").rstrip()
                if text:
                    yield Event("text", text + "\n")
                continue
            for ev in _codex_event(evt):
                yield ev

        if proc.returncode is None:
            try:
                remaining = _remaining(deadline)
                await asyncio.wait_for(
                    proc.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError from None
        else:
            await proc.wait()

        try:
            remaining = _remaining(deadline)
            err = await asyncio.wait_for(
                asyncio.shield(stderr_task), timeout=remaining)
        except asyncio.TimeoutError:
            raise TimeoutError from None
        if proc.returncode not in (0, None):
            stderr = err.decode(errors="replace").strip()
            yield Event("error", stderr or f"exit code {proc.returncode}")
        yield Event("done")


def _codex_event(evt: dict[str, Any]) -> list[Event]:
    """Map a Codex/agent stream-json record onto agora Events."""
    etype = evt.get("type") or evt.get("msg", {}).get("type", "")
    msg = evt.get("msg", evt)

    if etype in {"agent_message_delta", "agent_message_chunk", "message_delta"}:
        return [Event("text", msg.get("delta") or msg.get("text") or "")]
    if etype in {"agent_message", "assistant_message", "item.completed"}:
        text = msg.get("message") or msg.get("text") or ""
        if isinstance(msg.get("item"), dict):
            text = text or msg["item"].get("text", "")
        return [Event("text", text)] if text else []
    if etype in {"agent_reasoning", "agent_reasoning_delta"}:
        return [Event("thought", msg.get("text") or msg.get("delta") or "")]
    if "command" in etype or "tool" in etype or "exec" in etype:
        name = (msg.get("command") or msg.get("name")
                or msg.get("tool_name") or etype)
        if isinstance(name, list):
            name = " ".join(str(x) for x in name)
        return [Event("tool", str(name)[:120])]
    if etype == "error":
        return [Event("error", str(msg.get("message") or msg))]
    return []
