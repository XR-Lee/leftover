"""ACP runner: keeps a long-lived agent process and a real session.

Gives streaming text, visible tool calls and resumable sessions - which is
what makes heavy, long-running work usable from a chat client.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import queue as thread_queue
import sys
import threading
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

from .. import __version__
from ..config import AgentSpec
from .base import AcpIdleTimeout, BaseRunner, Event, OnEvent
from .process_tree import ProcessTree, terminate_process_tree

_DONE = object()
_ACTIVITY = object()
_CANCEL_RPC_TIMEOUT = 2.0
_CANCEL_GRACE_TIMEOUT = 2.0
_CLOSE_TIMEOUT = 6.0
_PROCESS_EXIT_TIMEOUT = 1.0
_FS_IO_TIMEOUT = 5.0
_FS_WORKER_COUNT = 2
_FS_QUEUE_LIMIT = 8
_SDK_SPAWN_AGENT_PROCESS = spawn_agent_process
_POSIX_SESSION_WRAPPER = (
    "import os,sys;os.setsid();"
    "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
)

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


class _FsJob:
    __slots__ = ("loop", "future", "func", "args", "cancelled")

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 future: asyncio.Future[Any], func: Any,
                 args: tuple[Any, ...]) -> None:
        self.loop = loop
        self.future = future
        self.func = func
        self.args = args
        self.cancelled = threading.Event()


class _DaemonWorkerPool:
    """Small bounded daemon pool that never joins during asyncio shutdown."""

    def __init__(self, workers: int, queue_limit: int) -> None:
        self.worker_count = max(1, workers)
        self.queue_limit = max(1, queue_limit)
        self._queue: thread_queue.Queue[_FsJob] = thread_queue.Queue(
            maxsize=self.queue_limit)
        self._start_lock = threading.Lock()
        self._started = False

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            for index in range(self.worker_count):
                threading.Thread(
                    target=self._worker,
                    name=f"leftover-acp-fs-{index + 1}",
                    daemon=True,
                ).start()
            self._started = True

    @staticmethod
    def _deliver(job: _FsJob, result: Any,
                 error: BaseException | None) -> None:
        if job.cancelled.is_set() or job.future.done():
            return
        if error is None:
            job.future.set_result(result)
        else:
            job.future.set_exception(error)

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job.cancelled.is_set():
                    continue
                result: Any = None
                error: BaseException | None = None
                try:
                    result = job.func(*job.args)
                except BaseException as exc:  # delivered on the owning loop
                    error = exc
                if not job.cancelled.is_set():
                    try:
                        job.loop.call_soon_threadsafe(
                            self._deliver, job, result, error)
                    except RuntimeError:
                        pass  # the timed-out owning loop has already closed
            finally:
                self._queue.task_done()

    async def run(self, func: Any, *args: Any, timeout: float) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        job = _FsJob(loop, future, func, args)
        self._ensure_started()
        try:
            self._queue.put_nowait(job)
        except thread_queue.Full:
            raise RuntimeError("ACP filesystem worker queue is full") from None
        try:
            return await asyncio.wait_for(future, timeout=max(timeout, 0.0))
        except asyncio.TimeoutError:
            job.cancelled.set()
            raise TimeoutError(
                f"ACP filesystem operation timed out after {timeout:g}s") from None
        except BaseException:
            job.cancelled.set()
            raise


_FS_WORKERS = _DaemonWorkerPool(_FS_WORKER_COUNT, _FS_QUEUE_LIMIT)


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    with contextlib.suppress(BaseException):
        task.exception()


def _cancel_and_drain(task: asyncio.Future[Any]) -> None:
    if not task.done():
        task.cancel()
    task.add_done_callback(_consume_task_result)


def _spawn_argv(command: list[str]) -> tuple[str, tuple[str, ...], bool]:
    """Wrap the real POSIX SDK spawn in a new session without changing PID."""
    isolated = (os.name == "posix"
                and spawn_agent_process is _SDK_SPAWN_AGENT_PROCESS)
    if isolated:
        return (
            sys.executable,
            ("-c", _POSIX_SESSION_WRAPPER, *command),
            True,
        )
    return command[0], tuple(command[1:]), False


def _capture_process_tree(proc: Any, *, isolated: bool) \
        -> ProcessTree | None:
    if proc is None or not callable(getattr(proc, "wait", None)):
        return None
    return ProcessTree.capture(proc, isolated=isolated)


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


async def _force_stop_process(
        proc: Any, tree: ProcessTree | None = None) -> None:
    tree = tree or _capture_process_tree(proc, isolated=False)
    if tree is None:
        return
    await terminate_process_tree(
        tree,
        term_timeout=_PROCESS_EXIT_TIMEOUT,
        kill_timeout=_PROCESS_EXIT_TIMEOUT,
    )


async def _close_transport_cleanup(
        stack: contextlib.AsyncExitStack | None, proc: Any,
        tree: ProcessTree | None) -> None:
    # The ACP SDK's context cleanup waits on connection tasks. Stop the known
    # child first so its pipes reach EOF and those tasks can finish before
    # aclose() is given its own finite grace period.
    try:
        await _force_stop_process(proc, tree)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        pass
    if stack is None:
        return
    try:
        await _await_hard(stack.aclose(), _CLOSE_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        pass


async def _close_transport(stack: contextlib.AsyncExitStack | None,
                           proc: Any,
                           tree: ProcessTree | None = None) -> None:
    cleanup = asyncio.create_task(
        _close_transport_cleanup(stack, proc, tree))
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        # Detach the caller immediately while the one cleanup task retains and
        # closes both the saved process tree and its SDK context.
        cleanup.add_done_callback(_consume_task_result)
        raise


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
            # Wake the consumer for protocol activity, but do not treat repeated
            # or non-renderable updates as user-visible idle progress.
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
        content = await _FS_WORKERS.run(
            _read_text_file_sync, path, line, limit, timeout=_FS_IO_TIMEOUT)
        return ReadTextFileResponse(content=content)

    async def write_text_file(self, session_id: str, path: str, content: str,
                              **kwargs: Any) -> WriteTextFileResponse | None:
        try:
            await _FS_WORKERS.run(
                _write_text_file_sync, path, content, timeout=_FS_IO_TIMEOUT)
        except TimeoutError:
            raise TimeoutError(
                f"ACP filesystem write timed out after {_FS_IO_TIMEOUT:g}s; "
                "the in-flight write outcome is uncertain and may complete "
                "later") from None
        return WriteTextFileResponse()


class AcpRunner(BaseRunner):
    def __init__(self, spec: AgentSpec) -> None:
        super().__init__(spec)
        self._stack: contextlib.AsyncExitStack | None = None
        self._proc: Any = None
        self._tree: ProcessTree | None = None
        self._opening_proc: Any = None
        self._opening_tree: ProcessTree | None = None
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
            tree: ProcessTree | None = None
            try:
                queue: asyncio.Queue[Any] = asyncio.Queue()
                bridge = _Bridge(queue)
                command, args, isolated = _spawn_argv(spec.acp_command)
                conn, proc = await stack.enter_async_context(
                    spawn_agent_process(
                        bridge,
                        command,
                        *args,
                        env={**os.environ, **spec.env},
                        cwd=requested_workdir,
                        transport_kwargs={"stderr": None},
                    )
                )
                tree = _capture_process_tree(proc, isolated=isolated)
                if epoch != self._lifecycle_epoch:
                    raise RuntimeError(
                        f"{spec.label}: ACP startup was superseded by close")
                self._opening_proc = proc
                self._opening_tree = tree
                await conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(
                        fs=FileSystemCapabilities(
                            read_text_file=True, write_text_file=True),
                        terminal=False,
                    ),
                    client_info=Implementation(name="leftover", version=__version__),
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
                    self._opening_tree = None
                try:
                    await _close_transport(stack, proc, tree)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
                raise

            self._stack = stack
            self._proc = proc
            self._tree = tree
            self._opening_proc = None
            self._opening_tree = None
            self._conn = conn
            self._session_id = session.session_id
            self._queue = queue

    def _invalidate_transport(
            self) -> tuple[contextlib.AsyncExitStack | None, Any,
                           ProcessTree | None,
                           asyncio.Task[Any] | None]:
        """Detach one ACP generation before any potentially blocking cleanup."""
        self._lifecycle_epoch += 1
        stack = self._stack
        if self._proc is not None:
            proc = self._proc
            tree = self._tree
        else:
            proc = self._opening_proc
            tree = self._opening_tree
        tree = tree or _capture_process_tree(proc, isolated=False)
        prompt_task = self._prompt_task
        self._stack = None
        self._proc = None
        self._tree = None
        self._opening_proc = None
        self._opening_tree = None
        self._conn = None
        self._session_id = None
        self._prompt_task = None
        self._queue = asyncio.Queue()
        return stack, proc, tree, prompt_task

    async def _retire_generation(self, conn: Any, session_id: str) -> None:
        """Close a failed prompt's generation without touching a newer one."""
        if self._conn is not conn or self._session_id != session_id:
            return
        stack, proc, tree, _prompt_task = self._invalidate_transport()
        try:
            await _close_transport(stack, proc, tree)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    async def stream(self, prompt: str, on_event: OnEvent | None = None
                     ) -> AsyncIterator[Event]:
        async with self._lock:                     # one prompt per session at a time
            # A previous queued prompt may retire the generation while this one
            # is waiting for the session lock. Recheck and rebuild only after
            # acquiring the lock so no caller can capture a detached connection.
            if self._conn is None:
                await self.start(self._workdir)
            assert self._conn is not None and self._session_id is not None
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
                    now = loop.time()
                    remaining = deadline - now
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
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
                    if item is _ACTIVITY:
                        if (idle_deadline is not None
                                and idle_deadline <= loop.time()):
                            raise AcpIdleTimeout(idle_timeout)
                        continue
                    if idle_deadline is not None:
                        idle_deadline = loop.time() + idle_timeout
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
        tree: ProcessTree | None = None
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
                stack, proc, tree, _prompt = self._invalidate_transport()
                invalidated = True
                try:
                    await _close_transport(stack, proc, tree)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            if not invalidated:
                stack, proc, tree, _prompt = self._invalidate_transport()
                cleanup = asyncio.create_task(
                    _close_transport(stack, proc, tree))
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
        stack, proc, tree, prompt_task = self._invalidate_transport()
        if prompt_task is not None:
            prompt_task.cancel()
        try:
            await _close_transport(stack, proc, tree)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        finally:
            if prompt_task is not None:
                await _cancel_task_bounded(
                    prompt_task, _CANCEL_GRACE_TIMEOUT)
