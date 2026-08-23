"""Terminal chrome for the leftover conversation. Quiet by default."""
from __future__ import annotations

import asyncio
import itertools
import os
import queue
import sys
import threading
from dataclasses import dataclass
from typing import TextIO

KIND_LABELS = {
    "plan": "plan",
    "computer_use": "computer use",
    "roundtable": "roundtable",
    "broadcast": "broadcast",
    "debate": "debate",
    "relay": "relay",
}


def _tty() -> bool:
    if os.environ.get("NO_COLOR", "") != "":
        return False
    return sys.stdout.isatty()


def remaining_bar(percent: float, width: int = 10) -> str:
    """usher doctor/why bar: remaining (not used) as █░."""
    filled = int(round(min(100.0, max(0.0, percent)) / 100.0 * width))
    filled = min(width, max(0, filled))
    return "█" * filled + "░" * (width - filled)


class _C:
    reset = "\033[0m"
    dim = "\033[2m"
    bold = "\033[1m"
    cyan = "\033[36m"
    yellow = "\033[33m"
    red = "\033[31m"
    green = "\033[32m"


def paint(text: str, *styles: str) -> str:
    if not _tty() or not styles:
        return text
    return "".join(styles) + text + _C.reset


def dim(text: str) -> str:
    return paint(text, _C.dim)


def bold(text: str) -> str:
    return paint(text, _C.bold)


def label(name: str) -> str:
    return paint(name, _C.bold, _C.cyan)


def warn(text: str) -> str:
    return paint(text, _C.yellow)


def err(text: str) -> str:
    return paint(text, _C.red)


def ok(text: str) -> str:
    return paint(text, _C.green)


def announce(name: str | None, kind: str = "") -> str:
    """Plain routing-entry line for JSON / skills. No fallback chain."""
    if not name:
        return "leftover · nobody available"
    extra = f" · {KIND_LABELS[kind]}" if kind in KIND_LABELS else ""
    return f"leftover · {name}{extra}"


def route_line(name: str, kind: str = "") -> str:
    """TTY version of announce: dim router, bold entry."""
    extra = KIND_LABELS.get(kind, "")
    line = dim("leftover · ") + label(name)
    if extra:
        line += dim(f" · {extra}")
    return line


def seat_bits(kind: str = "", *, headless: bool = False, sticky: bool = False,
              reason: str = "") -> list[str]:
    bits: list[str] = []
    label = KIND_LABELS.get(kind, kind.replace("_", " ") if kind else "coding")
    if label:
        bits.append(label)
    if headless:
        bits.append("headless")
    elif sticky:
        bits.append("sticky")
    elif reason:
        bits.append(reason)
    return bits


def arrow(name: str) -> str:
    return paint("→ ", _C.cyan) + label(name)


def seat_line(name: str, kind: str = "", *, headless: bool = False,
              sticky: bool = False, reason: str = "",
              override: str = "@name") -> str:
    """usher `→ claude  (debug task · quota OK · override with --agent)`."""
    bits = seat_bits(kind, headless=headless, sticky=sticky, reason=reason)
    if not headless:
        bits.append(f"override with {override}")
    meta = dim(f"({' · '.join(bits)})") if bits else ""
    line = arrow(name)
    return f"{line}  {meta}" if meta else line


def failover_line(src: str, dest: str, *, guarded: bool = True,
                  failure_kind: str = "") -> str:
    """usher `→ claude hit its cap — failing over to codex (...notice)`."""
    why = "hit its cap" if failure_kind == "quota" else "failed"
    note = " (with continuation notice)" if guarded else ""
    return warn(f"→ {src} {why} — failing over to {dest}{note}")


class _WriteTicket:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def claim(self) -> bool:
        with self._lock:
            return not self._cancelled


@dataclass
class _WriteJob:
    owner: "StreamSink"
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future
    ticket: _WriteTicket
    kind: str
    text: str


_WRITER_IDS = itertools.count(1)


class _DaemonStreamWriter:
    """Bounded daemon writer that keeps synchronous TextIO off the event loop."""

    def __init__(self, queue_size: int = 16) -> None:
        self.queue: queue.Queue[_WriteJob] = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    async def submit(self, owner: "StreamSink", kind: str, text: str) -> None:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        ticket = _WriteTicket()
        job = _WriteJob(owner, loop, future, ticket, kind, text)
        with self._lock:
            try:
                self.queue.put_nowait(job)
            except queue.Full as exc:
                raise RuntimeError("stream output queue is full") from exc
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name=f"leftover-stream-{next(_WRITER_IDS)}",
                    daemon=True,
                )
                self._thread.start()
        try:
            await future
        except BaseException:
            ticket.cancel()
            raise

    @staticmethod
    def _settle(future: asyncio.Future,
                error: BaseException | None) -> None:
        if future.done():
            return
        if error is None:
            future.set_result(None)
        else:
            future.set_exception(error)

    def _notify(self, job: _WriteJob,
                error: BaseException | None = None) -> None:
        try:
            job.loop.call_soon_threadsafe(self._settle, job.future, error)
        except RuntimeError:
            pass

    def _should_stop(self) -> bool:
        with self._lock:
            if not self.queue.empty():
                return False
            self._thread = None
            return True

    def _run(self) -> None:
        while True:
            try:
                job = self.queue.get(timeout=5)
            except queue.Empty:
                if self._should_stop():
                    return
                continue

            error: BaseException | None = None
            if job.ticket.claim():
                try:
                    job.owner._write_event(job.kind, job.text)
                except BaseException as exc:  # keep later jobs drainable
                    error = exc
            self._notify(job, error)
            self.queue.task_done()
            if job.kind == "done" and self._should_stop():
                return


_STREAM_WRITER = _DaemonStreamWriter()


class StreamSink:
    """Render one agent turn: badge, streamed text, compact tool lines."""

    def __init__(self, spec_label: str, out: TextIO = sys.stdout,
                 show_header: bool = True) -> None:
        self.spec_label = spec_label
        self.out = out
        self._header = not show_header
        self._nl = True
        self._writer = _STREAM_WRITER

    def _ensure_header(self) -> None:
        if self._header:
            return
        self.out.write(arrow(self.spec_label) + "\n")
        self.out.flush()
        self._header = True
        self._nl = True

    def _write_event(self, kind: str, text: str) -> None:
        if kind == "text" and text:
            self._ensure_header()
            self.out.write(text)
            self._nl = text.endswith("\n")
            self.out.flush()
        elif kind == "tool" and text:
            self._ensure_header()
            if not self._nl:
                self.out.write("\n")
            self.out.write(dim(f"  ▸ {text[:100]}") + "\n")
            self._nl = True
            self.out.flush()
        elif kind == "error" and text:
            self._ensure_header()
            if not self._nl:
                self.out.write("\n")
            self.out.write(err(f"  {text}") + "\n")
            self._nl = True
            self.out.flush()
        elif kind == "done":
            if self._header and not self._nl:
                self.out.write("\n")
                self._nl = True
            self.out.flush()

    async def __call__(self, ev) -> None:
        kind = getattr(ev, "kind", "")
        text = getattr(ev, "text", "") or ""
        await self._writer.submit(self, kind, text)


def setup_readline(path) -> None:
    try:
        import readline
    except ImportError:
        return
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(path)
    except OSError:
        pass
    readline.set_history_length(500)

    def _save() -> None:
        try:
            readline.write_history_file(path)
        except OSError:
            pass

    import atexit
    atexit.register(_save)
