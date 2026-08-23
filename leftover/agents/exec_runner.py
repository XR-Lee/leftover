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
from .process_tree import (
    ProcessTree,
    isolated_subprocess_kwargs,
    terminate_process_tree,
)


_TERMINATE_TIMEOUT = 1.0
_STDERR_CAPTURE_LIMIT = 64 * 1024
_PIPE_READ_SIZE = 64 * 1024
_PROCESS_POLL_INTERVAL = 0.01


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


async def _feed_stdin(
        writer: asyncio.StreamWriter, payload: bytes) -> None:
    """Write stdin while treating an early CLI exit like communicate()."""
    try:
        writer.write(payload)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await writer.wait_closed()


async def _wait_for_returncode(proc: asyncio.subprocess.Process) -> int:
    """Observe leader exit without waiting for inherited pipes to reach EOF."""
    while proc.returncode is None:
        await asyncio.sleep(_PROCESS_POLL_INTERVAL)
    return proc.returncode


async def _await_task_before(
        task: asyncio.Task[Any], deadline: float) -> Any:
    if task.done():
        return task.result()
    done, _pending = await asyncio.wait(
        {task}, timeout=_remaining(deadline))
    if task not in done:
        raise TimeoutError
    return task.result()


async def _readline_unbounded(reader: asyncio.StreamReader) -> bytes:
    """Read one line without StreamReader's default 64 KiB record limit."""
    chunks: list[bytes] = []
    while True:
        try:
            part = await reader.readuntil(b"\n")
        except asyncio.LimitOverrunError as exc:
            # readuntil leaves the oversized prefix buffered. Consume that
            # prefix and continue looking for the same record's newline.
            part = await reader.readexactly(exc.consumed)
            chunks.append(part)
        except asyncio.IncompleteReadError as exc:
            chunks.append(exc.partial)
            return b"".join(chunks)
        else:
            chunks.append(part)
            return b"".join(chunks)


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
        self._tree: ProcessTree | None = None
        self._proc_lock = asyncio.Lock()
        self._pipe_readers: dict[
            asyncio.subprocess.Process,
            dict[asyncio.StreamReader, asyncio.Task[bytes] | None],
        ] = {}
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
            tree = self._tree or ProcessTree.capture(
                proc, isolated=False)
            try:
                await self._reap(proc, tree, finalize_pipes=False)
            finally:
                if self._proc is proc:
                    self._proc = None
                    self._tree = None

    async def _release(
            self, proc: asyncio.subprocess.Process, tree: ProcessTree) -> None:
        async with self._proc_lock:
            try:
                await self._reap(proc, tree, finalize_pipes=True)
            finally:
                self._pipe_readers.pop(proc, None)
                if self._proc is proc:
                    self._proc = None
                    self._tree = None

    async def _reap(
            self, proc: asyncio.subprocess.Process, tree: ProcessTree,
            *, finalize_pipes: bool, graceful: bool = True) -> None:
        cleanup = asyncio.create_task(
            self._terminate_and_wait(
                proc,
                tree,
                finalize_pipes=finalize_pipes,
                graceful=graceful,
            ))
        try:
            await asyncio.shield(cleanup)
        except BaseException:
            # Cleanup must outlive cancellation of the caller that owns the CLI.
            with contextlib.suppress(BaseException):
                await cleanup
            raise

    def _register_pipe_task(
            self, proc: asyncio.subprocess.Process,
            reader: asyncio.StreamReader,
            task: asyncio.Task[bytes]) -> None:
        self._pipe_readers.setdefault(proc, {})[reader] = task

    def _register_inline_reader(
            self, proc: asyncio.subprocess.Process,
            reader: asyncio.StreamReader) -> None:
        self._pipe_readers.setdefault(proc, {})[reader] = None

    def _finish_inline_reader(
            self, proc: asyncio.subprocess.Process,
            reader: asyncio.StreamReader) -> asyncio.Task[bytes]:
        readers = self._pipe_readers.setdefault(proc, {})
        if readers.get(reader) is None:
            readers.pop(reader, None)
        return self._ensure_pipe_task(proc, reader)

    def _ensure_pipe_task(
            self, proc: asyncio.subprocess.Process,
            reader: asyncio.StreamReader) -> asyncio.Task[bytes]:
        readers = self._pipe_readers.setdefault(proc, {})
        task = readers.get(reader)
        if task is None and reader not in readers:
            task = asyncio.create_task(_drain_bounded_tail(reader))
            readers[reader] = task
        if task is None:
            raise RuntimeError("cannot drain a pipe with an active inline reader")
        return task

    def _ensure_pipe_drains(
            self, proc: asyncio.subprocess.Process) -> list[asyncio.Task[bytes]]:
        readers = self._pipe_readers.setdefault(proc, {})
        for reader in (proc.stdout, proc.stderr):
            if reader is not None and reader not in readers:
                readers[reader] = asyncio.create_task(
                    _drain_bounded_tail(reader))
        return [task for task in readers.values() if task is not None]

    @staticmethod
    def _close_stdin(proc: asyncio.subprocess.Process) -> None:
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()

    async def _settle_pipe_drains(
            self, proc: asyncio.subprocess.Process) -> None:
        tasks = self._ensure_pipe_drains(proc)
        if not tasks:
            return
        done, pending = await asyncio.wait(
            tasks, timeout=max(_TERMINATE_TIMEOUT, 0.0))
        for task in done:
            _consume_future(task)
        for task in pending:
            task.cancel()
            task.add_done_callback(_consume_future)

    async def _terminate_and_wait(
            self, proc: asyncio.subprocess.Process, tree: ProcessTree,
            *, finalize_pipes: bool, graceful: bool) -> None:
        self._close_stdin(proc)
        self._ensure_pipe_drains(proc)
        try:
            await terminate_process_tree(
                tree,
                term_timeout=_TERMINATE_TIMEOUT if graceful else 0.0,
                kill_timeout=_TERMINATE_TIMEOUT,
            )
        finally:
            if finalize_pipes:
                await self._settle_pipe_drains(proc)

    async def _spawn(self, *argv: str, stdin, stdout, stderr, cwd: str,
                     env: dict[str, str]
                     ) -> tuple[asyncio.subprocess.Process, ProcessTree]:
        async with self._proc_lock:
            if self._closed:
                raise RuntimeError(f"{self.spec.label}: exec runner is closed")
            if self._proc is not None and self._proc.returncode is None:
                raise RuntimeError(f"{self.spec.label}: exec process is already running")

            isolated = os.name == "posix"
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *argv,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                cwd=cwd,
                env=env,
                **isolated_subprocess_kwargs(),
            ))
            try:
                proc = await asyncio.shield(spawn)
            except BaseException:
                # Cancellation can arrive after the OS process exists but before
                # create_subprocess_exec returns it to us. Reap it if that race wins.
                with contextlib.suppress(BaseException):
                    proc = await spawn
                    tree = ProcessTree.capture(proc, isolated=isolated)
                    try:
                        await self._reap(
                            proc, tree, finalize_pipes=True)
                    finally:
                        self._pipe_readers.pop(proc, None)
                raise
            tree = ProcessTree.capture(proc, isolated=isolated)
            self._proc = proc
            self._tree = tree
            self._pipe_readers[proc] = {}
            return proc, tree

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
        proc, tree = await self._spawn(
            *argv,
            stdin=(asyncio.subprocess.PIPE
                   if stdin_data else asyncio.subprocess.DEVNULL),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workdir,
            env=env,
        )

        auxiliary_tasks: list[asyncio.Task[Any]] = []
        inline_stdout = False
        try:
            leader_task = asyncio.create_task(_wait_for_returncode(proc))
            auxiliary_tasks.append(leader_task)
            if stdin_data and proc.stdin is not None:
                stdin_task = asyncio.create_task(
                    _feed_stdin(proc.stdin, stdin_data))
                auxiliary_tasks.append(stdin_task)

            if spec.exec_output == "stream-json":
                assert proc.stdout is not None
                assert proc.stderr is not None
                stderr_task = asyncio.create_task(
                    _drain_bounded_tail(proc.stderr))
                self._register_pipe_task(proc, proc.stderr, stderr_task)
                self._register_inline_reader(proc, proc.stdout)
                inline_stdout = True
                async for ev in self._read_stream_json(
                        proc, tree, deadline, stderr_task, leader_task):
                    yield ev
                return

            assert proc.stdout is not None
            assert proc.stderr is not None
            stdout_task = asyncio.create_task(proc.stdout.read())
            stderr_task = asyncio.create_task(proc.stderr.read())
            self._register_pipe_task(proc, proc.stdout, stdout_task)
            self._register_pipe_task(proc, proc.stderr, stderr_task)

            await _await_task_before(leader_task, deadline)
            # Process.wait() can remain pending after the leader exits while a
            # descendant retains inherited pipes. Reap the saved process group
            # at the returncode boundary, then collect the already-produced data.
            await self._reap(
                proc, tree, finalize_pipes=False, graceful=False)
            _done, pending = await asyncio.wait(
                {stdout_task, stderr_task},
                timeout=max(_TERMINATE_TIMEOUT, 0.0),
            )
            if pending:
                raise TimeoutError

            out = stdout_task.result()
            err = stderr_task.result()

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
            if inline_stdout and proc.stdout is not None:
                # A timeout or malformed overlong NDJSON line can leave stdout
                # paused with unread bytes. Keep draining while terminate/kill
                # waits for the subprocess transport to reach EOF.
                self._finish_inline_reader(proc, proc.stdout)
            try:
                await self._release(proc, tree)
            finally:
                for task in auxiliary_tasks:
                    if not task.done():
                        task.cancel()
                        task.add_done_callback(_consume_future)
                    else:
                        _consume_future(task)

    async def _read_stream_json(self, proc: asyncio.subprocess.Process,
                                tree: ProcessTree, deadline: float,
                                stderr_task: asyncio.Task[bytes],
                                leader_task: asyncio.Task[int]
                                ) -> AsyncIterator[Event]:
        """Consume newline-delimited JSON events (Codex-style `exec --json`)."""
        assert proc.stdout is not None
        residual_group_reaped = False
        while True:
            line_task = asyncio.create_task(
                _readline_unbounded(proc.stdout))
            try:
                wait_timeout = (max(_TERMINATE_TIMEOUT, 0.0)
                                if leader_task.done()
                                else _remaining(deadline))
                done, _pending = await asyncio.wait(
                    {line_task, leader_task},
                    timeout=wait_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise TimeoutError
                if leader_task in done and not residual_group_reaped:
                    leader_task.result()
                    await self._reap(
                        proc, tree, finalize_pipes=False, graceful=False)
                    residual_group_reaped = True
                if line_task not in done:
                    done, _pending = await asyncio.wait(
                        {line_task}, timeout=max(_TERMINATE_TIMEOUT, 0.0))
                    if line_task not in done:
                        raise TimeoutError
                line = line_task.result()
            except BaseException:
                if not line_task.done():
                    line_task.cancel()
                with contextlib.suppress(BaseException):
                    await line_task
                raise

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

        if not leader_task.done():
            await _await_task_before(leader_task, deadline)
        else:
            leader_task.result()
        if not residual_group_reaped:
            await self._reap(
                proc, tree, finalize_pipes=False, graceful=False)

        done, _pending = await asyncio.wait(
            {stderr_task}, timeout=max(_TERMINATE_TIMEOUT, 0.0))
        if stderr_task not in done:
            raise TimeoutError
        err = stderr_task.result()
        if proc.returncode not in (0, None):
            stderr = err.decode(errors="replace").strip()
            yield Event("error", stderr or f"exit code {proc.returncode}")
        yield Event("done")


def _codex_event(evt: dict[str, Any]) -> list[Event]:
    """Map a Codex/agent stream-json record onto leftover Events."""
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
