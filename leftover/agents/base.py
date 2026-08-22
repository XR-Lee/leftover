"""Shared types for agent runners."""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

from ..config import AgentSpec


@dataclass
class Event:
    """A streamed fragment from an agent."""
    kind: str          # "text" | "thought" | "tool" | "error" | "done"
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


# Called with each Event as it arrives; used for live-editing Telegram messages.
OnEvent = Callable[[Event], Awaitable[None]]


class AcpIdleTimeout(TimeoutError):
    """An ACP prompt stayed alive but produced no updates for too long."""

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
        try:
            async with contextlib.aclosing(self.stream(prompt, on_event)) as stream:
                async for ev in stream:
                    if ev.kind == "text":
                        chunks.append(ev.text)
                    elif ev.kind == "tool" and ev.text:
                        turn.tools.append(ev.text)
                    elif ev.kind == "error":
                        turn.error = ev.text
                    if on_event is not None:
                        await _deliver_event(
                            on_event, ev, deadline - time.monotonic(),
                            float(self.spec.timeout))
        except AcpIdleTimeout as exc:
            turn.error = str(exc)
            timeout_kind = "idle"
        except EventSinkTimeout as exc:
            turn.error = str(exc)
            timeout_kind = "sink"
        except EventSinkFailure as exc:
            turn.error = exc.detail
            timeout_kind = "sink"
        except TimeoutError:
            turn.error = f"timed out after {self.spec.timeout}s"
            timeout_kind = "turn"
        except FileNotFoundError as exc:
            turn.error = f"CLI not found: {exc}"
        except Exception as exc:                      # noqa: BLE001
            turn.error = f"{type(exc).__name__}: {exc}"
        turn.text = "".join(chunks).strip()
        turn.seconds = time.monotonic() - started
        turn.meta = dict(getattr(self, "last_meta", {}) or {})
        if timeout_kind:
            turn.meta["timeout_kind"] = timeout_kind
        return turn
