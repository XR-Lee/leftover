"""ACP runner: keeps a long-lived agent process and a real session.

Gives streaming text, visible tool calls and resumable sessions - which is
what makes heavy, long-running work usable from a chat client.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import queue as thread_queue
import sys
import threading
from collections.abc import Awaitable
from dataclasses import dataclass
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
from .base import AcpIdleTimeout, BaseRunner, Event, OnEvent, child_env
from .process_tree import ProcessTree, terminate_process_tree

_DONE = object()
_ACTIVITY = object()
_TERMINAL_TOOL_STATUS = frozenset({
    "completed", "failed", "cancelled", "error", "success",
})
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
_OBSERVED_UPDATE_LIMIT = 1024


@dataclass(frozen=True, slots=True)
class _TurnTerminal:
    """Exactly-once terminal outcome for one ACP prompt epoch."""

    epoch: int
    state: str
    result: Any = None
    error: BaseException | None = None


class _TurnGate:
    """Routes updates to one prompt and closes permanently when it settles."""

    __slots__ = (
        "epoch", "session_id", "queue", "delivery", "text_chunks", "tools",
        "event_error", "terminal", "conn", "bridge", "generation",
        "cancel_requested", "cancel_attempt", "pump_task", "_accepting",
    )

    def __init__(self, epoch: int, session_id: str,
                 queue: asyncio.Queue[Any], *, conn: Any,
                 bridge: _Bridge | None, generation: object) -> None:
        self.epoch = epoch
        self.session_id = session_id
        self.queue = queue
        self.delivery: asyncio.Queue[Any] = asyncio.Queue()
        self.text_chunks: list[str] = []
        self.tools: list[str] = []
        self.event_error: str | None = None
        self.conn = conn
        self.bridge = bridge
        self.generation = generation
        self.cancel_requested = False
        self.cancel_attempt: asyncio.Task[bool] | None = None
        self.pump_task: asyncio.Task[None] | None = None
        self.terminal: asyncio.Future[_TurnTerminal] = (
            asyncio.get_running_loop().create_future())
        self._accepting = True

    def emit(self, item: Any) -> bool:
        if not self._accepting:
            return False
        self.queue.put_nowait(item)
        return True

    @property
    def accepting(self) -> bool:
        return self._accepting

    def settle(self, state: str, *, result: Any = None,
               error: BaseException | None = None) -> bool:
        if self.terminal.done():
            return False
        self._accepting = False
        terminal = _TurnTerminal(
            epoch=self.epoch, state=state, result=result, error=error)
        self.terminal.set_result(terminal)
        self.queue.put_nowait((_DONE, self.epoch))
        return True


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


def _field(obj: Any, *names: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        for name in names:
            value = obj.get(name)
            if value is not None:
                return value
        return None
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _collapse(text: str, limit: int = 120) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + " ..."


_PATH_KEYS = (
    "path", "file", "file_path", "filePath", "target_file", "targetFile", "uri",
)


def _location_path(location: Any) -> str:
    path = _field(location, "path", "uri")
    return str(path).strip() if path else ""


def _raw_input_hint(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in _PATH_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    command = raw.get("command", raw.get("cmd"))
    if isinstance(command, str) and command.strip():
        return command.strip()
    if isinstance(command, list) and command:
        return " ".join(str(part) for part in command if str(part).strip())
    return ""


def _tool_label(update: Any) -> str:
    title = str(
        _field(update, "title") or _field(update, "kind") or "tool"
    ).strip() or "tool"
    path = ""
    for location in _field(update, "locations") or []:
        path = _location_path(location)
        if path:
            break
    if not path:
        path = _raw_input_hint(_field(update, "raw_input", "rawInput"))
    if path and path not in title:
        return _collapse(f"{title} {path}")
    return _collapse(title)


def _tool_status(update: Any) -> str:
    return str(_field(update, "status") or "").strip().lower()


def _tool_label_better(new: str, old: str) -> bool:
    if not new or new == old:
        return False
    if not old or old in {"tool", "other"}:
        return True
    if old in new and len(new) > len(old):
        return True
    return ("/" in new or "\\" in new) and "/" not in old and "\\" not in old


def _plan_activity(update: Any) -> str:
    entries = _field(update, "entries")
    if entries is None:
        plan = _field(update, "plan")
        if plan is None:
            return ""
        entries = _field(plan, "entries")
        if entries is None:
            markdown = _field(plan, "content")
            if markdown:
                return _collapse(str(markdown))
            uri = _field(plan, "uri")
            return _collapse(str(uri)) if uri else ""
    in_progress: list[str] = []
    pending: list[str] = []
    for entry in entries or []:
        content = str(_field(entry, "content") or "").strip()
        if not content:
            continue
        status = str(_field(entry, "status") or "").strip()
        if status == "in_progress":
            in_progress.append(content)
        elif status == "pending":
            pending.append(content)
    chosen = in_progress or pending
    return _collapse(chosen[0]) if chosen else ""


def _update_payload(update: Any) -> Any:
    if isinstance(update, dict):
        return update
    model_dump = getattr(update, "model_dump", None)
    if callable(model_dump):
        return model_dump(
            mode="json", by_alias=True, exclude_none=True,
            exclude_unset=True)
    payload: dict[str, Any] = {}
    kind = getattr(update, "session_update", None)
    if kind is not None:
        payload["sessionUpdate"] = kind
    content = getattr(update, "content", None)
    if content is not None:
        text = _block_text(content)
        payload["content"] = {"text": text} if text else str(content)
    for attr, key in (
            ("tool_call_id", "toolCallId"),
            ("title", "title"),
            ("kind", "kind")):
        value = getattr(update, attr, None)
        if value is not None:
            payload[key] = value
    return payload or repr(update)


def _canonical_update_payload(value: Any) -> Any:
    """Normalize raw JSON and parsed ACP models to one fingerprint shape."""
    if isinstance(value, dict):
        return {
            str(key): _canonical_update_payload(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_update_payload(item) for item in value]
    return value


def _update_fingerprint(update: Any) -> str:
    return json.dumps(
        _canonical_update_payload(_update_payload(update)),
        sort_keys=True, separators=(",", ":"),
        default=str)


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
        self._seen_tools: dict[str, str] = {}
        self.in_flight_tools: set[str] = set()
        self._turn: _TurnGate | None = None
        self._observed_updates: list[
            tuple[str, str, _TurnGate | None]] = []
        self.dropped_updates = 0
        self._conn: Any = None

    def on_connect(self, conn: Any) -> None:  # acp calls this synchronously
        self._conn = conn

    def observe_stream(self, event: Any) -> None:
        """Capture turn ownership when a raw ACP notification arrives."""
        direction = getattr(getattr(event, "direction", None), "value", None)
        if direction != "incoming":
            return
        message = getattr(event, "message", None)
        if not isinstance(message, dict) or message.get("method") != "session/update":
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        session_id = params.get("sessionId", params.get("session_id", ""))
        update = params.get("update")
        self._observed_updates.append((
            str(session_id), _update_fingerprint(update), self._turn))
        overflow = len(self._observed_updates) - _OBSERVED_UPDATE_LIMIT
        if overflow > 0:
            del self._observed_updates[:overflow]

    def _claim_observed_turn(
            self, session_id: str, update: Any) -> tuple[bool, _TurnGate | None]:
        fingerprint = _update_fingerprint(update)
        for index, (observed_session, observed_update, turn) in enumerate(
                self._observed_updates):
            if (observed_session == session_id
                    and observed_update == fingerprint):
                del self._observed_updates[index]
                return True, turn
        return False, None

    def bind_turn(self, turn: _TurnGate) -> None:
        active = self._turn
        if active is not None and not active.terminal.done():
            raise RuntimeError("cannot replace an active ACP prompt epoch")
        self._turn = turn
        self.queue = turn.queue
        self._seen_tools.clear()
        self.in_flight_tools.clear()

    def settle_turn(self, turn: _TurnGate, state: str, *, result: Any = None,
                    error: BaseException | None = None) -> bool:
        settled = turn.settle(state, result=result, error=error)
        if self._turn is turn:
            self._turn = None
        return settled

    def session_update(self, session_id: str, update: Any,
                       **kwargs: Any) -> Awaitable[None]:
        # The raw stream observer captures ownership before the SDK schedules
        # its notification handler. Direct/fake clients fall back to the gate
        # visible when this method itself is called.
        observed, turn = self._claim_observed_turn(session_id, update)
        if not observed:
            # Direct/fake clients have no raw observer entry. With a real raw
            # entry, a mismatch is ambiguous and must not bind to a newer turn.
            turn = None if self._observed_updates else self._turn
        return self._session_update(turn, session_id, update, **kwargs)

    async def _session_update(self, turn: _TurnGate | None, session_id: str,
                              update: Any, **kwargs: Any) -> None:
        if (turn is None or turn.session_id != session_id
                or not turn.accepting):
            self.dropped_updates += 1
            return
        kind = str(_field(update, "session_update", "sessionUpdate") or "")
        event: Any = _ACTIVITY
        if kind == "agent_message_chunk":
            text = _block_text(_field(update, "content"))
            if text:
                event = Event("text", text)
        elif kind == "agent_thought_chunk":
            text = _block_text(_field(update, "content"))
            if text:
                event = Event("thought", text)
        elif kind in ("tool_call", "tool_call_update"):
            tool_id = str(_field(update, "tool_call_id", "toolCallId") or "") or "*"
            label = _tool_label(update)
            previous = self._seen_tools.get(tool_id)
            if previous is None:
                self._seen_tools[tool_id] = label
                event = Event("tool", label)
            elif _tool_label_better(label, previous):
                self._seen_tools[tool_id] = label
                event = Event("tool", label)
            if _tool_status(update) in _TERMINAL_TOOL_STATUS:
                self.in_flight_tools.discard(tool_id)
            else:
                self.in_flight_tools.add(tool_id)
        elif kind in ("plan", "plan_update"):
            text = _plan_activity(update)
            if text:
                event = Event("status", text)
        # Non-renderable activity wakes the consumer without extending the
        # user-visible idle deadline. A settled gate rejects all late updates.
        if not turn.emit(event):
            self.dropped_updates += 1

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
        self._bridge: _Bridge | None = None
        self._generation: object | None = None
        self._turn_epoch = 0
        self._active_turn: _TurnGate | None = None
        self._prompt_task: asyncio.Task[Any] | None = None
        self._lifecycle_epoch = 0
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def live_session(self) -> bool:
        return self._conn is not None and self._session_id is not None

    def _tools_in_flight(self) -> bool:
        bridge = self._bridge
        return bool(bridge is not None and bridge.in_flight_tools)

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
                        env=child_env(spec),
                        cwd=requested_workdir,
                        transport_kwargs={"stderr": None},
                        observers=[bridge.observe_stream],
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
            self._bridge = bridge
            self._generation = object()

    def _begin_turn(self, conn: Any, session_id: str) -> _TurnGate:
        self._turn_epoch += 1
        # Tests and embedders may install a fake connection directly. Give that
        # manually installed transport the same identity guarantees as start().
        if self._generation is None:
            self._generation = object()
        generation = self._generation
        queue = self._queue
        turn = _TurnGate(
            self._turn_epoch, session_id, queue, conn=conn,
            bridge=self._bridge, generation=generation)
        self._active_turn = turn
        if turn.bridge is not None:
            turn.bridge.bind_turn(turn)
        turn.pump_task = asyncio.create_task(
            self._pump_turn_events(turn),
            name=f"leftover-acp-events-{turn.epoch}",
        )
        turn.pump_task.add_done_callback(_consume_task_result)
        return turn

    def _owns_turn_generation(self, turn: _TurnGate) -> bool:
        return (
            self._active_turn is turn
            and self._generation is turn.generation
            and self._conn is turn.conn
            and self._session_id == turn.session_id
        )

    def _snapshot_turn_events(self, turn: _TurnGate) -> None:
        """Freeze every result-bearing event that precedes the terminal marker."""
        context = self._run_context.get()
        if context is None or context.settled:
            return
        context.chunks[:] = turn.text_chunks
        context.turn.tools[:] = turn.tools
        if turn.event_error is not None:
            context.turn.error = turn.event_error

    def _publish_turn_terminal(self, turn: _TurnGate) -> None:
        """Publish failure/cancellation after the ordered event pump catches up."""
        terminal = turn.terminal.result()
        if terminal.epoch != turn.epoch:
            return
        if terminal.state == "error":
            error = terminal.error
            detail = (f"{type(error).__name__}: {error}"
                      if error is not None else "ACP prompt failed")
            self._snapshot_turn_events(turn)
            self._settle_active_turn(error=detail)
        elif terminal.state == "cancelled":
            self._snapshot_turn_events(turn)
            self._settle_active_turn(
                error="stopped: cancelled", meta={"cancelled": True})

    async def _pump_turn_events(self, turn: _TurnGate) -> None:
        """Drain protocol events independently from a potentially blocked sink."""
        try:
            while True:
                item = await turn.queue.get()
                terminal = (
                    isinstance(item, tuple)
                    and len(item) == 2
                    and item[0] is _DONE
                    and item[1] == turn.epoch
                )
                if isinstance(item, Event):
                    if item.kind == "text":
                        turn.text_chunks.append(item.text)
                    elif item.kind == "tool" and item.text:
                        turn.tools.append(item.text)
                    elif item.kind == "error":
                        turn.event_error = item.text
                if terminal:
                    self._publish_turn_terminal(turn)
                turn.delivery.put_nowait(item)
                if terminal:
                    return
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._settle_turn(turn, "error", error=exc)
            self._publish_turn_terminal(turn)
            turn.delivery.put_nowait((_DONE, turn.epoch))

    def _settle_turn(self, turn: _TurnGate, state: str, *,
                     result: Any = None,
                     error: BaseException | None = None) -> bool:
        if turn.bridge is not None:
            return turn.bridge.settle_turn(
                turn, state, result=result, error=error)
        return turn.settle(state, result=result, error=error)

    async def _execute_prompt(self, conn: Any, session_id: str, prompt: str,
                              turn: _TurnGate) -> Any:
        try:
            result = await conn.prompt(
                session_id=session_id, prompt=[text_block(prompt)])
            # Let notification handlers already scheduled by a prompt response
            # finish against this gate before the terminal boundary closes it.
            await asyncio.sleep(0)
        except asyncio.CancelledError as exc:
            self._settle_turn(turn, "cancelled", error=exc)
            raise
        except Exception as exc:
            self._settle_turn(turn, "error", error=exc)
            raise
        state = ("cancelled"
                 if getattr(result, "stop_reason", "") == "cancelled"
                 else "completed")
        self._settle_turn(turn, state, result=result)
        return result

    def _invalidate_transport(
            self, expected_turn: _TurnGate | None = None
            ) -> tuple[contextlib.AsyncExitStack | None, Any,
                           ProcessTree | None,
                           asyncio.Task[Any] | None]:
        """Detach one ACP generation before any potentially blocking cleanup."""
        if (expected_turn is not None
                and not self._owns_turn_generation(expected_turn)):
            return None, None, None, None
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
        active_turn = self._active_turn
        if active_turn is not None:
            self._settle_turn(active_turn, "cancelled")
        self._stack = None
        self._proc = None
        self._tree = None
        self._opening_proc = None
        self._opening_tree = None
        self._conn = None
        self._session_id = None
        self._bridge = None
        self._generation = None
        self._active_turn = None
        self._prompt_task = None
        self._queue = asyncio.Queue()
        return stack, proc, tree, prompt_task

    async def _retire_generation(self, turn: _TurnGate) -> None:
        """Close a failed prompt's generation without touching a newer one."""
        if not self._owns_turn_generation(turn):
            return
        stack, proc, tree, _prompt_task = self._invalidate_transport(turn)
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
            turn = self._begin_turn(conn, session_id)
            queue = turn.delivery
            # The terminal future and bridge gate exist before the prompt task
            # can execute, so even an immediate response cannot lose wake-up.
            terminal_waiter = turn.terminal
            task = asyncio.create_task(
                self._execute_prompt(conn, session_id, prompt, turn)
            )
            task.add_done_callback(_consume_task_result)
            self._prompt_task = task

            try:
                loop = asyncio.get_running_loop()
                turn_timeout = max(float(self.spec.timeout), 0.0)
                deadline = loop.time() + turn_timeout
                idle_timeout = self.spec.acp_idle_timeout
                idle_deadline = (loop.time() + idle_timeout
                                 if idle_timeout > 0 else None)
                busy = False
                while True:
                    now = loop.time()
                    in_flight = self._tools_in_flight()
                    if in_flight:
                        # A long pytest is work. Do not spend the original
                        # start-of-turn budget while a tool is still running.
                        deadline = now + turn_timeout
                    remaining = deadline - now
                    if remaining <= 0:
                        raise TimeoutError
                    if idle_deadline is not None and busy and not in_flight:
                        # The last in-flight tool just ended. Restart hang
                        # detection from now so a 10-minute pytest is not an
                        # instant stall the moment it prints "done".
                        idle_deadline = now + idle_timeout
                    busy = in_flight
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        wait_timeout = remaining
                        if idle_deadline is not None and not in_flight:
                            idle_remaining = idle_deadline - now
                            if idle_remaining <= 0:
                                raise AcpIdleTimeout(idle_timeout)
                            wait_timeout = min(wait_timeout, idle_remaining)
                        try:
                            item = await asyncio.wait_for(
                                queue.get(), timeout=wait_timeout)
                        except asyncio.TimeoutError:
                            now = loop.time()
                            if self._tools_in_flight():
                                continue
                            if (idle_deadline is not None
                                    and idle_deadline <= now):
                                raise AcpIdleTimeout(idle_timeout) from None
                            raise TimeoutError from None
                    if (isinstance(item, tuple) and len(item) == 2
                            and item[0] is _DONE):
                        if item[1] == turn.epoch:
                            break
                        continue
                    if item is _ACTIVITY:
                        now = loop.time()
                        in_flight = self._tools_in_flight()
                        if idle_deadline is not None and busy and not in_flight:
                            # Completion of a long tool is not a stall. The
                            # deadline still dates from when that tool started.
                            idle_deadline = now + idle_timeout
                        busy = in_flight
                        if in_flight:
                            deadline = now + turn_timeout
                        if (idle_deadline is not None
                                and not in_flight
                                and idle_deadline <= now):
                            raise AcpIdleTimeout(idle_timeout)
                        continue
                    if idle_deadline is not None:
                        idle_deadline = loop.time() + idle_timeout
                    deadline = loop.time() + turn_timeout
                    yield item

                terminal = terminal_waiter.result()
                if terminal.state == "error":
                    exc = terminal.error
                    error = (f"{type(exc).__name__}: {exc}"
                             if exc is not None else "ACP prompt failed")
                    yield Event("error", error)
                    yield Event("done")
                    # BaseRunner has now consumed every queued event. Publish
                    # the failure before retiring this connection generation.
                    await self._retire_generation(turn)
                    return
                if terminal.state == "cancelled":
                    yield Event("error", "stopped: cancelled")
                elif terminal.state != "completed":
                    raise RuntimeError(
                        f"ACP prompt settled as {terminal.state} without "
                        "raising")
                elif getattr(terminal.result, "stop_reason", "") == "refusal":
                    yield Event("error", "stopped: refusal")
                yield Event("done")
                if turn.cancel_requested:
                    # A cancel notification has no server acknowledgement. Even
                    # if this prompt happened to finish normally, retire before
                    # releasing the session lock so delayed handling cannot hit
                    # the next prompt.
                    await self._retire_generation(turn)
            except BaseException as exc:
                if isinstance(exc, TimeoutError):
                    state = "timed_out"
                elif isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                    state = "cancelled"
                else:
                    state = "error"
                self._settle_turn(turn, state, error=exc)
                if isinstance(exc, AcpIdleTimeout):
                    self._settle_active_turn(
                        error=str(exc), timeout_kind="idle")
                elif isinstance(exc, TimeoutError):
                    self._settle_active_turn(
                        error=f"timed out after {self.spec.timeout}s",
                        timeout_kind="turn")
                await self._abort_prompt(task, turn)
                raise
            finally:
                self._settle_turn(turn, "cancelled")
                if self._active_turn is turn:
                    self._active_turn = None
                if self._queue is turn.queue:
                    self._queue = asyncio.Queue()
                if self._prompt_task is task:
                    self._prompt_task = None

    async def _abort_prompt(
            self, task: asyncio.Task[Any], turn: _TurnGate) -> None:
        """Establish a clean turn boundary before another prompt can start."""
        invalidated = False
        cancel_attempted = False
        stack: contextlib.AsyncExitStack | None = None
        proc: Any = None
        tree: ProcessTree | None = None
        try:
            attempted = turn.cancel_requested
            if attempted or not task.done():
                try:
                    await self._cancel_turn(turn)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
                cancel_attempted = turn.cancel_requested

            if not task.done():
                await asyncio.wait({task}, timeout=_CANCEL_GRACE_TIMEOUT)

            task_failed = task.cancelled()
            if task.done() and not task_failed:
                task_failed = task.exception() is not None

            owns_generation = self._owns_turn_generation(turn)
            if (owns_generation
                    and (cancel_attempted or not task.done() or task_failed)):
                # ACP updates carry no prompt id. Detaching the generation and
                # rotating its queue prevents late chunks or a late cancel RPC
                # from entering the next turn. ACP cancel is a session-wide
                # notification with no server acknowledgement, so even a
                # successful send makes the generation unsafe to reuse.
                stack, proc, tree, _prompt = self._invalidate_transport(turn)
                invalidated = True
                try:
                    await _close_transport(stack, proc, tree)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            owns_generation = self._owns_turn_generation(turn)
            if not invalidated and owns_generation:
                stack, proc, tree, _prompt = self._invalidate_transport(turn)
                cleanup = asyncio.create_task(
                    _close_transport(stack, proc, tree))
                cleanup.add_done_callback(_consume_task_result)
            raise
        finally:
            if task.done():
                _consume_task_result(task)
            else:
                await _cancel_task_bounded(task, _CANCEL_GRACE_TIMEOUT)

    async def _send_cancel(self, conn: Any, session_id: str) -> bool:
        try:
            await _await_hard(
                conn.cancel(session_id=session_id), _CANCEL_RPC_TIMEOUT)
        except asyncio.CancelledError:
            return False
        except Exception:  # noqa: BLE001
            return False
        return True

    async def _cancel_turn(self, turn: _TurnGate) -> bool:
        if turn.cancel_requested:
            attempt = turn.cancel_attempt
            if attempt is None:
                return False
            return await asyncio.shield(attempt)
        if not self._owns_turn_generation(turn):
            return False
        # A completed prompt already establishes its boundary. A session-wide
        # cancel sent afterward can arrive during the next prompt.
        if self._prompt_task is not None and self._prompt_task.done():
            return True
        # Pool and stream cleanup share one bounded RPC and its certainty
        # outcome. False means the generation must be retired before reuse.
        turn.cancel_requested = True
        if turn.conn is None or not turn.session_id:
            return False
        turn.cancel_attempt = asyncio.create_task(
            self._send_cancel(turn.conn, turn.session_id),
            name=f"leftover-acp-cancel-{turn.epoch}",
        )
        attempt = turn.cancel_attempt
        if attempt is None:
            return False
        return await asyncio.shield(attempt)

    async def cancel(self) -> bool:
        turn = self._active_turn
        if turn is None:
            return True
        return await self._cancel_turn(turn)

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
