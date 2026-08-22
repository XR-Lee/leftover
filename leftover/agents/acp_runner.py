"""ACP runner: keeps a long-lived agent process and a real session.

Gives streaming text, visible tool calls and resumable sessions - which is
what makes heavy, long-running work usable from a chat client.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
from collections.abc import Awaitable
from typing import Any, AsyncIterator

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.interfaces import Client
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    FileSystemCapabilities,
    Implementation,
    ReadTextFileResponse,
    RequestPermissionResponse,
    WriteTextFileResponse,
)

from ..config import AgentSpec
from .base import AcpIdleTimeout, BaseRunner, Event, OnEvent

_DONE = object()
_ACTIVITY = object()
_CANCEL_RPC_TIMEOUT = 2.0
_CANCEL_GRACE_TIMEOUT = 2.0
_CLOSE_TIMEOUT = 6.0
_PROCESS_EXIT_TIMEOUT = 1.0

# Permission kinds we are willing to auto-accept, best first.
_ALLOW_ORDER = ("allow_always", "allow_once", "allow")


def _block_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(content, dict):
        return content.get("text", "")
    return ""


def _read_text_file_sync(path: str, line: int | None,
                         limit: int | None) -> str:
    start = max((line or 1) - 1, 0)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for _index in range(start):
            if not fh.readline():
                return ""
        if limit is not None and limit < 0:
            return ""
        if limit:
            return "".join(itertools.islice(fh, limit))
        return fh.read()


def _write_text_file_sync(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    with contextlib.suppress(BaseException):
        task.exception()


def _cancel_and_drain(task: asyncio.Future[Any]) -> None:
    if not task.done():
        task.cancel()
    task.add_done_callback(_consume_task_result)


async def _await_hard(awaitable: Awaitable[Any], timeout: float) -> Any:
    """Bound the caller without extending the deadline for task cleanup."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=max(timeout, 0.0))
    except BaseException:
        _cancel_and_drain(task)
        raise
    if task not in done:
        _cancel_and_drain(task)
        raise TimeoutError
    return task.result()


async def _cancel_task_bounded(task: asyncio.Task[Any], timeout: float) -> None:
    if not task.done():
        task.cancel()
    try:
        done, _pending = await asyncio.wait({task}, timeout=max(timeout, 0.0))
    except BaseException:
        task.add_done_callback(_consume_task_result)
        raise
    if task in done:
        _consume_task_result(task)
    else:
        task.add_done_callback(_consume_task_result)


def _signal_process(proc: Any, *, force: bool) -> None:
    if proc is None or getattr(proc, "returncode", None) is not None:
        return
    method = getattr(proc, "kill" if force else "terminate", None)
    if callable(method):
        with contextlib.suppress(ProcessLookupError, OSError, RuntimeError):
            method()


async def _force_stop_process(proc: Any) -> None:
    wait = getattr(proc, "wait", None)
    if proc is None or not callable(wait):
        return
    if getattr(proc, "returncode", None) is not None:
        return
    _signal_process(proc, force=False)
    try:
        await _await_hard(wait(), _PROCESS_EXIT_TIMEOUT)
        return
    except asyncio.CancelledError:
        _signal_process(proc, force=True)
        raise
    except Exception:  # noqa: BLE001
        pass
    _signal_process(proc, force=True)
    try:
        await _await_hard(wait(), _PROCESS_EXIT_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        pass


async def _close_transport(stack: contextlib.AsyncExitStack | None,
                           proc: Any) -> None:
    # The ACP SDK's context cleanup waits on connection tasks. Stop the known
    # child first so its pipes reach EOF and those tasks can finish before
    # aclose() is given its own finite grace period.
    await _force_stop_process(proc)
    if stack is not None:
        try:
            await _await_hard(stack.aclose(), _CLOSE_TIMEOUT)
        except asyncio.CancelledError:
            _signal_process(proc, force=True)
            raise
        except Exception:  # noqa: BLE001
            pass


class _Bridge(Client):
    """Receives session/update notifications and funnels them into a queue."""

    def __init__(self, queue: asyncio.Queue[Any], trust_tools: bool = True) -> None:
        self.queue = queue
        self.trust_tools = trust_tools
        self._seen_tools: set[str] = set()
        self._conn: Any = None

    def on_connect(self, conn: Any) -> None:  # acp calls this synchronously
        self._conn = conn

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        kind = getattr(update, "session_update", "")
        emitted = False
        if kind == "agent_message_chunk":
            text = _block_text(getattr(update, "content", None))
            if text:
                await self.queue.put(Event("text", text))
                emitted = True
        elif kind == "agent_thought_chunk":
            text = _block_text(getattr(update, "content", None))
            if text:
                await self.queue.put(Event("thought", text))
                emitted = True
        elif kind in ("tool_call", "tool_call_update"):
            tool_id = str(getattr(update, "tool_call_id", ""))
            title = getattr(update, "title", None) or getattr(update, "kind", "") or "tool"
            if tool_id not in self._seen_tools:
                self._seen_tools.add(tool_id)
                await self.queue.put(Event("tool", str(title)[:120]))
                emitted = True
        if not emitted:
            # Even an update with no user-visible rendering proves the agent is
            # alive, including repeated updates for an already shown tool.
            await self.queue.put(_ACTIVITY)

    async def request_permission(self, session_id: str, tool_call: Any,
                                 options: list[Any], **kwargs: Any
                                 ) -> RequestPermissionResponse:
        if self.trust_tools:
            for wanted in _ALLOW_ORDER:
                for opt in options:
                    if str(getattr(opt, "kind", "")).lower() == wanted:
                        return RequestPermissionResponse(
                            outcome=AllowedOutcome(outcome="selected",
                                                   option_id=opt.option_id))
            if options:
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected",
                                           option_id=options[0].option_id))
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def read_text_file(self, session_id: str, path: str, line: int | None = None,
                             limit: int | None = None, **kwargs: Any
                             ) -> ReadTextFileResponse:
        content = await asyncio.to_thread(
            _read_text_file_sync, path, line, limit)
        return ReadTextFileResponse(content=content)

    async def write_text_file(self, session_id: str, path: str, content: str,
                              **kwargs: Any) -> WriteTextFileResponse | None:
        await asyncio.to_thread(_write_text_file_sync, path, content)
        return WriteTextFileResponse()


class AcpRunner(BaseRunner):
    def __init__(self, spec: AgentSpec) -> None:
        super().__init__(spec)
        self._stack: contextlib.AsyncExitStack | None = None
        self._proc: Any = None
        self._opening_proc: Any = None
        self._conn: Any = None
        self._session_id: str | None = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._prompt_task: asyncio.Task[Any] | None = None
        self._lifecycle_epoch = 0
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def live_session(self) -> bool:
        return self._conn is not None and self._session_id is not None

    async def start(self, workdir: str) -> None:
        async with self._lifecycle_lock:
            requested_workdir = os.path.realpath(os.path.expanduser(workdir))
            if self._conn is not None:
                if requested_workdir != self._workdir:
                    raise RuntimeError(
                        f"{self.spec.label}: ACP session is already running in "
                        f"{self._workdir}; close it before switching to "
                        f"{requested_workdir}")
                return
            await super().start(requested_workdir)
            spec = self.spec
            if not spec.acp_command:
                raise RuntimeError(f"{spec.label}: no acp_command configured")

            epoch = self._lifecycle_epoch
            stack = contextlib.AsyncExitStack()
            proc: Any = None
            try:
                queue: asyncio.Queue[Any] = asyncio.Queue()
                bridge = _Bridge(queue)
                conn, proc = await stack.enter_async_context(
                    spawn_agent_process(
                        bridge,
                        spec.acp_command[0],
                        *spec.acp_command[1:],
                        env={**os.environ, **spec.env},
                        cwd=requested_workdir,
                        transport_kwargs={"stderr": None},
                    )
                )
                if epoch != self._lifecycle_epoch:
                    raise RuntimeError(
                        f"{spec.label}: ACP startup was superseded by close")
                self._opening_proc = proc
                await conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(
                        fs=FileSystemCapabilities(
                            read_text_file=True, write_text_file=True),
                        terminal=False,
                    ),
                    client_info=Implementation(name="agora", version="0.1.0"),
                )
                if epoch != self._lifecycle_epoch:
                    raise RuntimeError(
                        f"{spec.label}: ACP startup was superseded by close")
                session = await conn.new_session(
                    cwd=requested_workdir, mcp_servers=[])
                if epoch != self._lifecycle_epoch:
                    raise RuntimeError(
                        f"{spec.label}: ACP startup was superseded by close")
            except BaseException:
                if self._opening_proc is proc:
                    self._opening_proc = None
                try:
                    await _close_transport(stack, proc)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
                raise

            self._stack = stack
            self._proc = proc
            self._opening_proc = None
            self._conn = conn
            self._session_id = session.session_id
            self._queue = queue

    def _invalidate_transport(
            self) -> tuple[contextlib.AsyncExitStack | None, Any,
                           asyncio.Task[Any] | None]:
        """Detach one ACP generation before any potentially blocking cleanup."""
        self._lifecycle_epoch += 1
        stack = self._stack
        proc = self._proc if self._proc is not None else self._opening_proc
        prompt_task = self._prompt_task
        self._stack = None
        self._proc = None
        self._opening_proc = None
        self._conn = None
        self._session_id = None
        self._prompt_task = None
        self._queue = asyncio.Queue()
        return stack, proc, prompt_task

    async def _retire_generation(self, conn: Any, session_id: str) -> None:
        """Close a failed prompt's generation without touching a newer one."""
        if self._conn is not conn or self._session_id != session_id:
            return
        stack, proc, _prompt_task = self._invalidate_transport()
        try:
            await _close_transport(stack, proc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    async def stream(self, prompt: str, on_event: OnEvent | None = None
                     ) -> AsyncIterator[Event]:
        if self._conn is None:
            await self.start(self._workdir)
        assert self._conn is not None and self._session_id is not None

        async with self._lock:                     # one prompt per session at a time
            conn = self._conn
            session_id = self._session_id
            queue = self._queue
            while not queue.empty():               # drop anything stale
                queue.get_nowait()

            done_token = object()
            task = asyncio.create_task(
                conn.prompt(session_id=session_id, prompt=[text_block(prompt)])
            )
            self._prompt_task = task
            task.add_done_callback(
                lambda _t, token=done_token, target=queue: target.put_nowait(
                    (_DONE, token)))

            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.spec.timeout
                idle_timeout = self.spec.acp_idle_timeout
                idle_deadline = (loop.time() + idle_timeout
                                 if idle_timeout > 0 else None)
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        now = loop.time()
                        remaining = deadline - now
                        if remaining <= 0:
                            raise TimeoutError
                        wait_timeout = remaining
                        if idle_deadline is not None:
                            idle_remaining = idle_deadline - now
                            if idle_remaining <= 0:
                                raise AcpIdleTimeout(idle_timeout)
                            wait_timeout = min(wait_timeout, idle_remaining)
                        try:
                            item = await asyncio.wait_for(
                                queue.get(), timeout=wait_timeout)
                        except asyncio.TimeoutError:
                            now = loop.time()
                            if (idle_deadline is not None
                                    and idle_deadline <= now
                                    and idle_deadline <= deadline):
                                raise AcpIdleTimeout(idle_timeout) from None
                            raise TimeoutError from None
                    if (isinstance(item, tuple) and len(item) == 2
                            and item[0] is _DONE):
                        if item[1] is done_token:
                            break
                        continue
                    if idle_deadline is not None:
                        idle_deadline = loop.time() + idle_timeout
                    if item is _ACTIVITY:
                        continue
                    yield item

                try:
                    result = await task
                except Exception as exc:               # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"
                    await self._retire_generation(conn, session_id)
                    yield Event("error", error)
                else:
                    reason = getattr(result, "stop_reason", "")
                    if reason in ("refusal", "cancelled"):
                        yield Event("error", f"stopped: {reason}")
                yield Event("done")
            except BaseException:
                await self._abort_prompt(task)
                raise
            finally:
                if self._prompt_task is task:
                    self._prompt_task = None

    async def _abort_prompt(self, task: asyncio.Task[Any]) -> None:
        """Establish a clean turn boundary before another prompt can start."""
        invalidated = False
        stack: contextlib.AsyncExitStack | None = None
        proc: Any = None
        try:
            if not task.done():
                try:
                    await self.cancel()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    pass

            if not task.done():
                await asyncio.wait({task}, timeout=_CANCEL_GRACE_TIMEOUT)

            if not task.done():
                # ACP updates carry no prompt id. Detaching the generation and
                # rotating its queue prevents late chunks entering the next turn.
                stack, proc, _prompt = self._invalidate_transport()
                invalidated = True
                try:
                    await _close_transport(stack, proc)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            if not invalidated:
                stack, proc, _prompt = self._invalidate_transport()
                cleanup = asyncio.create_task(_close_transport(stack, proc))
                cleanup.add_done_callback(_consume_task_result)
            raise
        finally:
            if task.done():
                _consume_task_result(task)
            else:
                await _cancel_task_bounded(task, _CANCEL_GRACE_TIMEOUT)

    async def cancel(self) -> None:
        conn = self._conn
        session_id = self._session_id
        if conn is not None and session_id:
            try:
                await _await_hard(
                    conn.cancel(session_id=session_id), _CANCEL_RPC_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        stack, proc, prompt_task = self._invalidate_transport()
        if prompt_task is not None:
            prompt_task.cancel()
        try:
            await _close_transport(stack, proc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        finally:
            if prompt_task is not None:
                await _cancel_task_bounded(
                    prompt_task, _CANCEL_GRACE_TIMEOUT)
