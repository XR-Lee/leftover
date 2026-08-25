"""Shared types for agent runners."""
from __future__ import annotations

import asyncio
import contextlib
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol
from uuid import uuid4

from ..config import AgentSpec


@dataclass
class Event:
    """A streamed fragment from an agent."""
    kind: str          # "text" | "thought" | "tool" | "status" | "error" | "done"
    text: str = ""


@dataclass
class Turn:
    """The complete result of one agent speaking once."""
    agent: AgentSpec
    text: str = ""
    tools: list[str] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0
    # Raw payload from the CLI when it gives us one (is_error, usage, cost).
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())

    def short(self, limit: int = 900) -> str:
        body = self.text.strip() or (self.error or "(no output)")
        return body if len(body) <= limit else body[:limit].rstrip() + " ..."


@dataclass
class _RunContext:
    turn: Turn
    chunks: list[str]
    started: float
    settled: bool = False


class TurnState(str, Enum):
    """Lifecycle state for one submitted agent turn."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            self.COMPLETED,
            self.ERROR,
            self.TIMED_OUT,
            self.CANCELLED,
        }


def _turn_state(turn: Turn) -> TurnState:
    if turn.meta.get("shutdown_interrupted") or turn.meta.get("cancelled"):
        return TurnState.CANCELLED
    if turn.meta.get("timeout_kind"):
        return TurnState.TIMED_OUT
    if turn.error:
        return TurnState.ERROR
    return TurnState.COMPLETED


class TurnHandle:
    """One-shot completion handle for a submitted agent turn.

    ``wait(timeout=...)`` only bounds that observation. It never cancels the
    underlying turn; cancellation is an explicit ``cancel()`` operation.
    """

    def __init__(
            self, agent: AgentSpec,
            parent_id: str | None = None,
            on_settled: Callable[["TurnHandle"], None] | None = None) -> None:
        loop = asyncio.get_running_loop()
        self.turn_id = uuid4().hex
        self.parent_id = parent_id
        self.agent = agent
        self.state = TurnState.QUEUED
        self.created_at = time.monotonic()
        self.started_at: float | None = None
        self.deadline_at: float | None = None
        self.settled_at: float | None = None
        self.result: Turn | None = None
        self.exception: BaseException | None = None
        self._done: asyncio.Future[Turn] = loop.create_future()
        self._cleanup_done: asyncio.Future[None] = loop.create_future()
        self._task: asyncio.Task[None] | None = None
        self._on_settled = on_settled
        self._done.add_done_callback(_consume_task_result)

    def __await__(self):
        return self.wait().__await__()

    def done(self) -> bool:
        return self._done.done()

    def cleanup_done(self) -> bool:
        return self._cleanup_done.done()

    async def wait(self, timeout: float | None = None) -> Turn:
        """Wait for settlement without making timeout a task cancellation."""
        if self._done.done():
            return self._done.result()
        if timeout is not None and timeout <= 0:
            raise TimeoutError
        waiter = asyncio.shield(self._done)
        if timeout is None:
            return await waiter
        return await asyncio.wait_for(waiter, timeout)

    async def wait_cleanup(self, timeout: float | None = None) -> None:
        """Wait until the pool worker's bounded cleanup phase has ended.

        A runner may still detach a low-level task after its own hard cleanup
        deadline, but the serialized turn slot and pool ownership are released.
        """
        if self._cleanup_done.done():
            return self._cleanup_done.result()
        if timeout is not None and timeout <= 0:
            raise TimeoutError
        waiter = asyncio.shield(self._cleanup_done)
        if timeout is None:
            await waiter
        else:
            await asyncio.wait_for(waiter, timeout)

    def cancel(self) -> bool:
        """Settle as cancelled and request cancellation of the pool worker."""
        settled = self._settle_cancelled()
        self._cancel_worker()
        return settled

    def _cancel_worker(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _bind_task(self, task: asyncio.Task[None]) -> None:
        if self._task is not None:
            raise RuntimeError("turn handle already has a worker")
        self._task = task

    def _mark_running(self, timeout: float) -> bool:
        if self.state is not TurnState.QUEUED:
            return False
        self.started_at = time.monotonic()
        self.deadline_at = self.started_at + max(timeout, 0.0)
        self.state = TurnState.RUNNING
        return True

    def _settle_result(self, turn: Turn) -> bool:
        return self._settle(_turn_state(turn), result=turn)

    def _settle_cancelled(
            self, *, error: str = "cancelled",
            meta: dict[str, Any] | None = None) -> bool:
        if self.done():
            return False
        now = time.monotonic()
        started = self.started_at if self.started_at is not None else self.created_at
        turn_meta = {"cancelled": True}
        if meta:
            turn_meta.update(meta)
        turn = Turn(
            agent=self.agent,
            error=error,
            seconds=now - started,
            meta=turn_meta,
        )
        return self._settle(TurnState.CANCELLED, result=turn)

    def _settle_exception(self, exc: BaseException) -> bool:
        return self._settle(TurnState.ERROR, exception=exc)

    def _settle(
            self, state: TurnState, *, result: Turn | None = None,
            exception: BaseException | None = None) -> bool:
        if not state.terminal:
            raise ValueError(f"non-terminal settlement state: {state.value}")
        if self.done():
            return False
        self.state = state
        self.result = result
        self.exception = exception
        self.settled_at = time.monotonic()
        if exception is not None:
            self._done.set_exception(exception)
        elif result is not None:
            self._done.set_result(result)
        else:
            self._done.set_exception(
                RuntimeError("turn settled without a result or exception"))
        if self._on_settled is not None:
            self._on_settled(self)
        return True

    def _mark_cleanup_done(self) -> None:
        if not self._cleanup_done.done():
            self._cleanup_done.set_result(None)


# Called with each Event as it arrives; used for live-editing Telegram messages.
OnEvent = Callable[[Event], Awaitable[None]]


class AcpIdleTimeout(TimeoutError):
    """An ACP prompt produced no visible progress and has no in-flight tool."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(
            f"ACP idle timed out after {seconds:g}s without an update")


class EventSinkTimeout(TimeoutError):
    """Delivering a streamed event consumed the turn's remaining deadline."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(f"event sink timed out within the {seconds:g}s turn limit")


class EventSinkFailure(RuntimeError):
    """A UI/network sink raised while receiving a streamed event."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    with contextlib.suppress(BaseException):
        task.exception()


async def _deliver_event(on_event: OnEvent, event: Event, timeout: float,
                         turn_timeout: float) -> None:
    """Bound a UI/network sink without extending its cancellation cleanup."""
    if timeout <= 0:
        raise EventSinkTimeout(turn_timeout)
    task = asyncio.create_task(on_event(event))
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except BaseException:
        if not task.done():
            task.cancel()
        task.add_done_callback(_consume_task_result)
        raise
    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise EventSinkTimeout(turn_timeout)
    try:
        task.result()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - preserve the sink's useful type
        raise EventSinkFailure(f"{type(exc).__name__}: {exc}") from exc


class Runner(Protocol):
    """Drives one CLI agent."""

    spec: AgentSpec

    async def start(self, workdir: str) -> None: ...

    def stream(self, prompt: str, on_event: OnEvent | None = None
               ) -> AsyncIterator[Event]: ...

    async def run(self, prompt: str, on_event: OnEvent | None = None) -> Turn: ...

    async def cancel(self) -> None: ...

    async def close(self) -> None: ...


class BaseRunner:
    """Common run()/timing logic shared by the ACP and exec runners."""

    def __init__(self, spec: AgentSpec) -> None:
        self.spec = spec
        self._workdir = "."
        self._turn_settler: ContextVar[
            Callable[[Turn], None] | None] = ContextVar(
                f"leftover_turn_settler_{id(self)}", default=None)
        self._run_context: ContextVar[_RunContext | None] = ContextVar(
            f"leftover_run_context_{id(self)}", default=None)

    def _bind_turn_settler(
            self, settler: Callable[[Turn], None]) -> Token:
        return self._turn_settler.set(settler)

    def _unbind_turn_settler(self, token: Token) -> None:
        self._turn_settler.reset(token)

    def _finalize_active_turn(self) -> Turn | None:
        context = self._run_context.get()
        if context is None:
            return None
        turn = context.turn
        turn.text = "".join(context.chunks).strip()
        turn.seconds = time.monotonic() - context.started
        turn.meta = {
            **dict(getattr(self, "last_meta", {}) or {}),
            **turn.meta,
        }
        return turn

    def _settle_active_turn(
            self, *, error: str | None = None,
            timeout_kind: str = "",
            meta: dict[str, Any] | None = None) -> Turn | None:
        """Publish a known terminal result before runner cleanup finishes."""
        context = self._run_context.get()
        if context is None:
            return None
        if context.settled:
            return context.turn
        turn = context.turn
        if error is not None:
            turn.error = error
        if timeout_kind:
            turn.meta["timeout_kind"] = timeout_kind
        if meta:
            turn.meta.update(meta)
        self._finalize_active_turn()
        context.settled = True
        settler = self._turn_settler.get()
        if settler is not None:
            settler(turn)
        return turn

    async def wait_cleanup(self) -> None:
        """Wait for runner-owned cleanup not already covered by run()."""
        return None

    def live_session(self) -> bool:
        """True when this runner holds a native agent session (ACP)."""
        return False

    async def start(self, workdir: str) -> None:
        self._workdir = workdir

    async def cancel(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def run(self, prompt: str, on_event: OnEvent | None = None) -> Turn:
        started = time.monotonic()
        deadline = started + max(float(self.spec.timeout), 0.0)
        turn = Turn(agent=self.spec)
        chunks: list[str] = []
        timeout_kind = ""
        context = _RunContext(turn=turn, chunks=chunks, started=started)
        context_token = self._run_context.set(context)
        try:
            try:
                async with contextlib.aclosing(
                        self.stream(prompt, on_event)) as stream:
                    async for ev in stream:
                        if not context.settled:
                            if ev.kind == "text":
                                chunks.append(ev.text)
                            elif ev.kind == "tool" and ev.text:
                                turn.tools.append(ev.text)
                            elif ev.kind == "error":
                                turn.error = ev.text
                        if on_event is not None:
                            try:
                                await _deliver_event(
                                    on_event, ev,
                                    deadline - time.monotonic(),
                                    float(self.spec.timeout))
                            except EventSinkTimeout as exc:
                                self._settle_active_turn(
                                    error=str(exc), timeout_kind="sink")
                                raise
                            except EventSinkFailure as exc:
                                self._settle_active_turn(
                                    error=exc.detail, timeout_kind="sink")
                                raise
            except AcpIdleTimeout as exc:
                if not context.settled:
                    turn.error = str(exc)
                timeout_kind = "idle"
            except EventSinkTimeout as exc:
                if not context.settled:
                    turn.error = str(exc)
                timeout_kind = "sink"
            except EventSinkFailure as exc:
                if not context.settled:
                    turn.error = exc.detail
                timeout_kind = "sink"
            except TimeoutError:
                if not context.settled:
                    turn.error = f"timed out after {self.spec.timeout}s"
                timeout_kind = "turn"
            except FileNotFoundError as exc:
                if not context.settled:
                    turn.error = f"CLI not found: {exc}"
            except Exception as exc:                      # noqa: BLE001
                if not context.settled:
                    turn.error = f"{type(exc).__name__}: {exc}"
            if not context.settled:
                if timeout_kind:
                    turn.meta["timeout_kind"] = timeout_kind
                self._finalize_active_turn()
                # Child tasks inherit ContextVars. Seal the shared context so a
                # late inherited child cannot mutate or republish this result.
                context.settled = True
            return turn
        finally:
            self._run_context.reset(context_token)
