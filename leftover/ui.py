"""Terminal chrome for the leftover conversation. Quiet by default."""
from __future__ import annotations

import asyncio
from collections import deque
import io
import itertools
import os
import queue
import re
import shutil
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Sequence, TextIO

KIND_LABELS = {
    "plan": "plan",
    "computer_use": "computer use",
    "heavy": "heavy",
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


def compact_activity(text: str, limit: int = 120) -> str:
    """One progress line: whitespace collapsed, truncated."""
    collapsed = " ".join(_safe_terminal_text(text).split())
    if len(collapsed) <= limit:
        return collapsed
    clusters: list[str] = []
    used = 0
    for cluster in _graphemes(collapsed):
        if used + len(cluster) > limit:
            break
        clusters.append(cluster)
        used += len(cluster)
    return "".join(clusters).rstrip() + " ..."


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _safe_terminal_text(text: object) -> str:
    """Remove controls that could escape a single terminal progress line."""
    cleaned: list[str] = []
    for char in _ANSI_ESCAPE.sub("", str(text or "")):
        category = unicodedata.category(char)
        codepoint = ord(char)
        if char in "\r\n\t" or category in {"Zl", "Zp"}:
            cleaned.append(" ")
        elif char == "\x1b" or category in {"Cc", "Cs"}:
            continue
        elif (category == "Cf" and char not in {"\u200c", "\u200d"}
              and not 0xE0020 <= codepoint <= 0xE007F):
            continue
        else:
            cleaned.append(char)
    return "".join(cleaned)


def _is_grapheme_extend(char: str) -> bool:
    codepoint = ord(char)
    return (unicodedata.category(char) in {"Mn", "Me"}
            or 0x1F3FB <= codepoint <= 0x1F3FF
            or 0xE0020 <= codepoint <= 0xE007F)


def _is_regional_indicator(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def _is_emoji_base(char: str) -> bool:
    codepoint = ord(char)
    return (0x1F000 <= codepoint <= 0x1FAFF
            or 0x2600 <= codepoint <= 0x27BF)


def _graphemes(text: str):
    """Yield the terminal-relevant grapheme clusters used by roster text."""
    index = 0
    while index < len(text):
        cluster = [text[index]]
        regional = _is_regional_indicator(text[index])
        index += 1
        if (regional and index < len(text)
                and _is_regional_indicator(text[index])):
            cluster.append(text[index])
            index += 1
        while index < len(text):
            char = text[index]
            if _is_grapheme_extend(char):
                cluster.append(char)
                index += 1
                continue
            if char == "\u200d" and index + 1 < len(text):
                cluster.extend((char, text[index + 1]))
                index += 2
                continue
            break
        yield "".join(cluster)


def _grapheme_width(cluster: str) -> int:
    visible = [
        char for char in cluster
        if not _is_grapheme_extend(char)
        and char not in {"\u200c", "\u200d"}
        and not unicodedata.category(char).startswith("C")
    ]
    if not visible:
        return 0
    if ("\ufe0f" in cluster or "\u20e3" in cluster
            or (len(visible) == 2
                and all(_is_regional_indicator(char) for char in visible))
            or ("\u200d" in cluster
                and any(_is_emoji_base(char) for char in visible))):
        return 2
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in visible
    )


def _display_width(text: str) -> int:
    """Return terminal cells, not Python code points."""
    plain = _ANSI_ESCAPE.sub("", text)
    return sum(_grapheme_width(cluster) for cluster in _graphemes(plain))


def _pad_cells(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


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
    owner: object
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future
    ticket: _WriteTicket
    kind: str
    text: object


_WRITER_IDS = itertools.count(1)


class _DaemonStreamWriter:
    """Bounded daemon writer that keeps synchronous TextIO off the event loop."""

    def __init__(self, queue_size: int = 16,
                 thread_name_prefix: str = "leftover-stream") -> None:
        self.queue: queue.Queue[_WriteJob] = queue.Queue(maxsize=queue_size)
        self.thread_name_prefix = thread_name_prefix
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    async def submit(self, owner: object, kind: str, text: object) -> None:
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
                    name=(f"{self.thread_name_prefix}-"
                          f"{next(_WRITER_IDS)}"),
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
                    job.owner._write_event(job.kind, job.text, job.ticket)
                except BaseException as exc:  # keep later jobs drainable
                    error = exc
            self._notify(job, error)
            self.queue.task_done()
            if job.kind == "done" and self._should_stop():
                return


_STREAM_WRITER = _DaemonStreamWriter()
_ROSTER_WRITER = _DaemonStreamWriter(
    thread_name_prefix="leftover-roster")


class StreamSink:
    """Render one agent turn: badge, streamed text, compact tool/status lines."""

    def __init__(self, spec_label: str, out: TextIO = sys.stdout,
                 show_header: bool = True) -> None:
        self.spec_label = spec_label
        self.out = out
        self._header = not show_header
        self._nl = True
        self._thought = ""
        self._writer = _STREAM_WRITER

    def _ensure_header(self) -> None:
        if self._header:
            return
        self.out.write(arrow(self.spec_label) + "\n")
        self.out.flush()
        self._header = True
        self._nl = True

    def _write_note(self, text: str, bullet: str = "▸") -> None:
        text = compact_activity(text, 100)
        if not text:
            return
        self._ensure_header()
        if not self._nl:
            self.out.write("\n")
        self.out.write(dim(f"  {bullet} {text}") + "\n")
        self._nl = True
        self.out.flush()

    def _flush_thought(self) -> None:
        text = compact_activity(self._thought)
        self._thought = ""
        if text:
            self._write_note(text, "·")

    def _write_event(self, kind: str, text: str,
                     _ticket: _WriteTicket | None = None) -> None:
        if kind == "thought" and text:
            self._thought += text
            collapsed = compact_activity(self._thought)
            if "\n" in text or len(collapsed) >= 120:
                self._flush_thought()
            return
        if kind != "thought":
            self._flush_thought()
        if kind == "text" and text:
            self._ensure_header()
            self.out.write(text)
            self._nl = text.endswith("\n")
            self.out.flush()
        elif kind == "tool" and text:
            self._write_note(text, "▸")
        elif kind == "status" and text:
            self._write_note(text, "·")
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


_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _stream_tty(out: TextIO) -> bool:
    return hasattr(out, "isatty") and bool(out.isatty())


@dataclass
class RosterRow:
    key: str
    label: str
    badge: str = ""
    seat_key: str = ""
    role: str = ""
    state: str = "queued"
    detail: str = ""
    started_at: float | None = None
    seconds: float | None = None
    tools: int = 0
    output_chars: int = 0
    turn_id: str = ""
    _last_tool: str = ""
    _last_log: tuple[str, str] = ("", "")
    _last_log_at: float = 0.0


class Roster:
    """Compact lifecycle view for one multi-agent discussion phase."""

    _FINISHED = {
        "ready", "done", "failed", "timed_out", "cancelled", "empty",
    }
    _WRITE_QUEUE_SIZE = 16

    def __init__(
            self, specs=(), *, title: str = "", mode: str = "",
            out: TextIO | None = None, spin: int = 0,
            width: int | None = None, heartbeat_seconds: float = 30.0,
            close_timeout: float = 0.25, clock=None) -> None:
        self.out = sys.stderr if out is None else out
        self.mode = _safe_terminal_text(mode)
        self.title = _safe_terminal_text(title)
        self.spin = spin
        reported_tty = _stream_tty(self.out)
        # Arbitrary TextIO and POSIX TTY writes cannot be revoked after they
        # start. Destructive redraw is therefore restricted to the exact,
        # synchronous in-memory sink used by deterministic snapshot tests.
        self._tty = reported_tty and type(self.out) is io.StringIO
        self._width = width
        self.heartbeat_seconds = max(0.0, heartbeat_seconds)
        self.close_timeout = max(0.0, close_timeout)
        self._clock = time.monotonic if clock is None else clock
        self._rows: dict[str, RosterRow] = {}
        self._order: list[str] = []
        self._seat_current: dict[str, str] = {}
        self._actual_current: dict[str, str] = {}
        self._drawn = 0
        self._phase_index = 1
        self._phase_total = 1
        self._phase_epoch = 0
        self._parallel = True
        self._phase_started = self._clock()
        self._last_visible = self._phase_started
        self._ticker: asyncio.Task | None = None
        self._writer = _ROSTER_WRITER
        self._writes: deque[tuple[str, object]] = deque()
        self._write_task: asyncio.Task | None = None
        self._closed = False
        for spec in specs:
            self._ensure(spec, spec, "")

    @staticmethod
    def _spec_key(spec) -> str:
        return str(getattr(spec, "key", "") or spec)

    def _ensure(self, seat, spec, role: str) -> RosterRow:
        seat_key = self._spec_key(seat)
        key = self._spec_key(spec)
        row_id = f"{seat_key}:{key}"
        if row_id not in self._rows:
            label = _safe_terminal_text(getattr(spec, "label", key) or key)
            badge = _safe_terminal_text(
                getattr(spec, "emoji", "") or label[:1] or "*")
            self._rows[row_id] = RosterRow(
                key=key, label=label, badge=badge,
                seat_key=seat_key, role=_safe_terminal_text(role),
            )
            self._order.append(row_id)
        row = self._rows[row_id]
        if role:
            row.role = _safe_terminal_text(role)
        self._actual_current[key] = row_id
        self._seat_current.setdefault(seat_key, row_id)
        return row

    def ensure(self, spec) -> RosterRow:
        return self._ensure(spec, spec, "")

    async def begin_phase(
            self, *, mode: str, title: str, index: int, total: int,
            seats: Sequence[tuple[object, str]], parallel: bool) -> None:
        if self._ticker is not None:
            await self.end_phase()
        self._closed = False
        self._phase_epoch += 1
        self.mode = _safe_terminal_text(mode)
        self.title = _safe_terminal_text(title)
        self._phase_index = index
        self._phase_total = max(1, total)
        self._parallel = parallel
        self._rows.clear()
        self._order.clear()
        self._seat_current.clear()
        self._actual_current.clear()
        self._drawn = 0
        self.spin = 0
        self._phase_started = self._clock()
        self._last_visible = self._phase_started
        for spec, role in seats:
            self._ensure(spec, spec, role)
        if self._tty:
            self._redraw(force=True)
        else:
            now = self._clock()
            initial_rows = self._seat_rows()
            for row in initial_rows:
                row._last_log = (row.state, row.detail)
                row._last_log_at = now
            self._write("\n".join([
                self._phase_log("started"),
                *(self._row_log(row, now) for row in initial_rows),
            ]))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._tty or self.heartbeat_seconds > 0:
            self._ticker = loop.create_task(
                self._tick_loop(), name="leftover-group-progress")

    def _start_attempt(self, seat, spec, role: str) -> RosterRow:
        now = self._clock()
        seat_key = self._spec_key(seat)
        actual_key = self._spec_key(spec)
        row_id = f"{seat_key}:{actual_key}"
        known_attempt = row_id in self._rows
        previous_id = self._seat_current.get(seat_key)
        previous = self._rows.get(previous_id or "")
        if previous is not None and previous.key != actual_key:
            previous.state = "replaced"
            replacement = _safe_terminal_text(
                getattr(spec, "label", actual_key) or actual_key)
            previous.detail = f"continued by {replacement}"
            previous.seconds = self._elapsed(previous, now)
            self._redraw(changed=previous, force=True)
        row = self._ensure(seat, spec, role)
        self._seat_current[seat_key] = row_id
        row.state = "queued"
        row.detail = "waiting for execution slot"
        row.started_at = now
        row.seconds = None
        row.tools = 0
        row.output_chars = 0
        row.turn_id = ""
        if not known_attempt:
            self._redraw(changed=row, force=True)
        return row

    def sink(self, seat, role: str = ""):
        epoch = self._phase_epoch

        async def start(spec):
            if self._closed or epoch != self._phase_epoch:
                async def ignore(_ev) -> None:
                    return None

                ignore.leftover_lifecycle = True  # type: ignore[attr-defined]
                return ignore
            row = self._start_attempt(seat, spec, role)

            async def on_event(ev) -> None:
                if self._closed or epoch != self._phase_epoch:
                    return
                self._event(row, ev)

            on_event.leftover_lifecycle = True  # type: ignore[attr-defined]
            return on_event

        return start

    def as_sink(self):
        async def start(spec):
            return await self.sink(spec)(spec)

        return start

    def _event(self, row: RosterRow, ev) -> None:
        kind = str(getattr(ev, "kind", "") or "")
        text = str(getattr(ev, "text", "") or "")
        data = getattr(ev, "data", {}) or {}
        if kind == "lifecycle":
            state = str(data.get("state") or text)
            row.turn_id = str(data.get("turn_id") or row.turn_id)
            if state == "queued":
                if row.state == "queued":
                    row.detail = "waiting for execution slot"
                else:
                    return
            elif state == "preparing":
                self._set(row, "preparing", "starting CLI session")
            elif state == "running":
                self._set(row, "running", "waiting for live update")
            return
        if kind in {"thought", "status"} and text:
            self._set(row, "thinking", text)
        elif kind == "tool" and text:
            if text != row._last_tool:
                row.tools += 1
                row._last_tool = text
            self._set(row, "working", text, force=True)
        elif kind == "text" and text:
            row.output_chars += len(text)
            if row.state != "answering":
                self._set(row, "answering", "streaming answer")
        elif kind == "error" and text:
            self._set(row, "failed", text, force=True)
        elif kind == "done" and row.state not in {
                "failed", "timed_out", "cancelled", "replaced"}:
            self._set(row, "ready", "answer buffered", force=True)

    def _set(self, row: RosterRow, state: str, detail: str = "",
             *, force: bool = False) -> None:
        detail = compact_activity(detail, 160)
        changed = row.state != state or row.detail != detail
        row.state = state
        row.detail = detail
        if changed or force:
            self._redraw(changed=row, force=force)

    async def finish(self, seat, turn, role: str = "") -> None:
        if self._closed:
            return
        seat_key = self._spec_key(seat)
        actual = getattr(turn, "agent", seat)
        actual_key = self._spec_key(actual)
        row_id = self._seat_current.get(seat_key)
        row = self._rows.get(row_id or "")
        if row is None or row.key != actual_key:
            row = self._start_attempt(seat, actual, role)
        meta = dict(getattr(turn, "meta", {}) or {})
        error = str(getattr(turn, "error", "") or "")
        if meta.get("cancelled") or meta.get("shutdown_interrupted"):
            state, detail = "cancelled", error or "stopped"
        elif meta.get("timeout_kind") or meta.get("queue_timeout"):
            state, detail = "timed_out", error or "timed out"
        elif error:
            state, detail = "failed", error
        elif bool(getattr(turn, "ok", False)):
            state, detail = "ready", "answer buffered"
        else:
            state, detail = "empty", "no answer"
        seconds = float(getattr(turn, "seconds", 0.0) or 0.0)
        row.seconds = seconds if seconds > 0 else self._elapsed(row)
        row.tools = len(getattr(turn, "tools", ()) or ())
        self._set(row, state, detail, force=True)

    def mark(self, spec, state: str, detail: str = "") -> None:
        if self._closed:
            return
        row = self._rows.get(self._actual_current.get(self._spec_key(spec), ""))
        if row is None:
            row = self.ensure(spec)
        aliases = {
            "run": "running", "tool": "working", "fail": "failed",
        }
        mapped = _safe_terminal_text(aliases.get(state, state))
        if mapped not in {"queued", "replaced"} and row.started_at is None:
            row.started_at = self._clock()
        self._set(row, mapped, detail, force=True)

    def lines(self) -> list[str]:
        width = self._columns()
        now = self._clock()
        header = self._header(now)
        rows = [dim(self._clip("  " + header, width))]
        rows.append(dim(self._clip("  " + "─" * max(0, width - 2), width)))
        names = [f"{row.badge} {row.label}" if row.badge else row.label
                 for row in self._rows.values()]
        name_width = min(16, max(
            [8, *(_display_width(name) for name in names)]))
        role_width = min(12, max(
            [4, *(_display_width(row.role) for row in self._rows.values())]))
        show_role = width >= 64 and any(
            row.role for row in self._rows.values())
        for row_id in self._order:
            row = self._rows[row_id]
            marker, state = self._mark(row)
            name = f"{row.badge} {row.label}" if row.badge else row.label
            name = self._clip(name, name_width)
            prefix = f"  {_pad_cells(name, name_width)}  "
            if show_role:
                shown_role = self._clip(row.role, role_width)
                prefix += f"{_pad_cells(shown_role, role_width)}  "
            prefix += f"{marker} {state:<9}"
            elapsed = self._format_elapsed(self._elapsed(row, now)) \
                if row.started_at is not None or row.seconds is not None else ""
            suffix = f"  {elapsed:>5}" if elapsed else ""
            detail = self._row_detail(row)
            available = (width - _display_width(prefix)
                         - _display_width(suffix) - 2)
            middle = f"  {self._clip(detail, available)}" \
                if detail and available > 2 else ""
            rows.append(self._clip(prefix + middle + suffix, width))
        return rows

    def freeze(self) -> None:
        if self._tty:
            self._queue_write("roster_freeze", "")

    async def end_phase(self) -> None:
        ticker = self._ticker
        self._ticker = None
        if ticker is not None:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
        final_rows: list[RosterRow] = []
        for row_id in self._seat_current.values():
            row = self._rows.get(row_id)
            if row is None:
                continue
            if row.state == "ready":
                row.state = "done"
                final_rows.append(row)
            elif row.state not in self._FINISHED:
                row.state = "cancelled"
                row.detail = "interrupted"
                row.seconds = self._elapsed(row)
                final_rows.append(row)
        if self._tty:
            self._redraw(force=True)
            self.freeze()
        else:
            now = self._clock()
            for row in final_rows:
                row._last_log = (row.state, row.detail)
                row._last_log_at = now
            self._write("\n".join([
                *(self._row_log(row, now) for row in final_rows),
                self._phase_log("complete"),
            ]))
        await self.close()

    async def _tick_loop(self) -> None:
        interval = 0.25 if self._tty else max(0.01, self.heartbeat_seconds)
        while True:
            await asyncio.sleep(interval)
            if self._tty:
                self.spin += 1
                self._redraw(force=True)
            elif (self.heartbeat_seconds > 0
                  and self._clock() - self._last_visible
                  >= self.heartbeat_seconds):
                self._write(
                    "leftover: still working (" + self._phase_summary() + ")")

    def _mark(self, row: RosterRow) -> tuple[str, str]:
        marks = {
            "queued": ("·", "queued"),
            "starting": (_SPIN[self.spin % len(_SPIN)], "starting"),
            "preparing": (_SPIN[self.spin % len(_SPIN)], "preparing"),
            "running": (_SPIN[self.spin % len(_SPIN)], "running"),
            "thinking": (_SPIN[self.spin % len(_SPIN)], "thinking"),
            "working": ("▸", "working"),
            "answering": ("…", "answering"),
            "ready": ("✓", "ready"),
            "done": ("✓", "done"),
            "failed": ("!", "failed"),
            "timed_out": ("!", "timeout"),
            "cancelled": ("×", "stopped"),
            "empty": ("○", "empty"),
            "replaced": ("↪", "replaced"),
        }
        return marks.get(row.state, ("·", row.state or "queued"))

    def _redraw(self, changed: RosterRow | None = None,
                *, force: bool = False) -> None:
        if not self._tty:
            if changed is None:
                return
            now = self._clock()
            signature = (changed.state, changed.detail)
            if signature == changed._last_log:
                return
            if (not force and changed.state in {"thinking", "running"}
                    and now - changed._last_log_at < 1.5):
                return
            changed._last_log = signature
            changed._last_log_at = now
            self._write(self._row_log(changed, now))
            return
        lines = self.lines()
        block = "\n".join(lines) + "\n"
        self._queue_write("roster_snapshot", (block, len(lines)))

    def _write_event(self, kind: str, payload: object,
                     _ticket: _WriteTicket | None = None) -> None:
        try:
            if kind == "roster_snapshot":
                block, line_count = payload
                prefix = f"\033[{self._drawn}A\033[J" if self._drawn else ""
                text = prefix + str(block)
                next_drawn = int(line_count)
            elif kind == "roster_freeze":
                text = "\n" if self._drawn else ""
                next_drawn = 0
            else:
                text = str(payload).rstrip() + "\n"
                next_drawn = self._drawn
            self.out.write(text)
            self.out.flush()
            self._drawn = next_drawn
        except (OSError, ValueError):
            return
        self._last_visible = self._clock()

    def _write(self, text: str) -> None:
        self._queue_write("roster_line", text)

    def _queue_write(self, kind: str, payload: object) -> None:
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_event(kind, payload)
            return

        item = (kind, payload)
        if (kind == "roster_snapshot" and self._writes
                and self._writes[-1][0] == "roster_snapshot"):
            self._writes[-1] = item
        else:
            if len(self._writes) >= self._WRITE_QUEUE_SIZE:
                self._writes.popleft()
            self._writes.append(item)
        if self._write_task is None or self._write_task.done():
            self._write_task = loop.create_task(
                self._drain_writes(), name="leftover-roster-output")

    async def _drain_writes(self) -> None:
        try:
            while self._writes:
                item = self._writes.popleft()
                try:
                    await self._writer.submit(self, *item)
                except RuntimeError as exc:
                    if "queue is full" not in str(exc):
                        continue
                    self._writes.appendleft(item)
                    await asyncio.sleep(0.01)
                except Exception:  # terminal output is best-effort
                    continue
        finally:
            self._write_task = None
            if self._writes and not self._closed:
                self._write_task = asyncio.create_task(
                    self._drain_writes(), name="leftover-roster-output")

    async def flush(self) -> bool:
        """Flush queued frames, abandoning them if TextIO stays blocked."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.close_timeout
        while True:
            task = self._write_task
            if task is None:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(asyncio.shield(task), remaining)
            except (TimeoutError, asyncio.TimeoutError):
                break
            if self._write_task is None and not self._writes:
                return True
        self._writes.clear()
        task = self._write_task
        if task is not None and not task.done():
            # A claimed TextIO call cannot be interrupted; cancellation keeps
            # later coalesced frames from following it after the deadline.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return False

    async def close(self) -> bool:
        """Seal the phase against later events, then flush bounded output."""
        self._closed = True
        return await self.flush()

    def _columns(self) -> int:
        columns = self._width
        if columns is None:
            try:
                columns = os.get_terminal_size(self.out.fileno()).columns
            except (AttributeError, OSError, TypeError, ValueError):
                columns = shutil.get_terminal_size(
                    fallback=(88, 24)).columns
        return max(1, min(120, int(columns)))

    @staticmethod
    def _clip(text: str, width: int) -> str:
        if width <= 0:
            return ""
        text = _safe_terminal_text(text)
        if _display_width(text) <= width:
            return text
        target = width if width <= 3 else width - 3
        clusters: list[str] = []
        used = 0
        for cluster in _graphemes(text):
            cluster_width = _grapheme_width(cluster)
            if used + cluster_width > target:
                break
            clusters.append(cluster)
            used += cluster_width
        clipped = "".join(clusters).rstrip()
        return clipped if width <= 3 else clipped + "..."

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        if seconds < 60:
            return f"{seconds}s"
        minutes, second = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes}:{second:02d}"
        hours, minute = divmod(minutes, 60)
        return f"{hours}:{minute:02d}h"

    def _elapsed(self, row: RosterRow, now: float | None = None) -> float:
        if row.seconds is not None:
            return row.seconds
        if row.started_at is None:
            return 0.0
        return max(0.0, (self._clock() if now is None else now) - row.started_at)

    def _seat_rows(self) -> list[RosterRow]:
        return [self._rows[row_id] for row_id in self._seat_current.values()
                if row_id in self._rows]

    def _counts(self) -> tuple[int, int, int]:
        rows = self._seat_rows()
        finished = sum(row.state in self._FINISHED for row in rows)
        failed = sum(row.state in {
            "failed", "timed_out", "cancelled", "empty"} for row in rows)
        return finished, failed, len(rows)

    def _phase_summary(self) -> str:
        finished, failed, total = self._counts()
        name = self.mode or self.title or "discussion"
        phase = f"{name} {self._phase_index}/{self._phase_total}"
        summary = f"{phase} · {finished}/{total} finished"
        if failed:
            summary += f" · {failed} failed"
        return summary

    def _header(self, now: float) -> str:
        finished, failed, total = self._counts()
        bits = [self.mode or self.title or "discussion"]
        if self.mode:
            bits.append(f"phase {self._phase_index}/{self._phase_total}")
            if self.title:
                bits.append(self.title)
        bits.append(f"{finished}/{total} finished")
        if failed:
            bits.append(f"{failed} failed")
        bits.append("parallel" if self._parallel else "sequential")
        bits.append(self._format_elapsed(now - self._phase_started))
        return " · ".join(bits)

    def _row_detail(self, row: RosterRow) -> str:
        if row.state in {"ready", "done"}:
            if row.tools:
                return f"{row.tools} tool" + ("s" if row.tools != 1 else "")
            return "answer ready"
        return row.detail

    def _row_log(self, row: RosterRow, now: float) -> str:
        _marker, state = self._mark(row)
        bits = [self._phase_summary(), row.label]
        if row.role:
            bits.append(row.role)
        bits.append(state)
        detail = self._row_detail(row)
        if detail:
            bits.append(detail)
        if row.started_at is not None or row.seconds is not None:
            bits.append(self._format_elapsed(self._elapsed(row, now)))
        return "leftover: " + " · ".join(bits)

    def _phase_log(self, state: str) -> str:
        elapsed = self._format_elapsed(self._clock() - self._phase_started)
        flow = "parallel" if self._parallel else "sequential"
        return (f"leftover: {self.mode} · phase "
                f"{self._phase_index}/{self._phase_total} · {self.title} · "
                f"{self._phase_summary().split(' · ', 1)[-1]} · "
                f"{flow} · {state} · {elapsed}")


REPL_COMMANDS = (
    "/plan", "/cu", "/computer", "/computer-use",
    "/heavy", "/discuss",
    "/rt", "/roundtable", "/all", "/debate", "/relay",
    "/quota", "/scope", "/cd", "/who", "/reset", "/help", "/quit", "/exit",
)
REPL_HINT = "tab  /heavy /plan /cu /rt /debate /relay   /quota /scope /cd /quit"
SCOPE_WORDS = ("on", "off")


def mention_tokens(agents) -> list[str]:
    """@key and @alias for enabled agents, first-seen order."""
    seen: set[str] = set()
    tokens: list[str] = []
    for agent in agents:
        if not getattr(agent, "enabled", True):
            continue
        for raw in (agent.key, *getattr(agent, "aliases", ())):
            token = f"@{str(raw).lstrip('@')}"
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            tokens.append(token)
    return tokens


class Completer:
    """Tab-complete leftover slash commands, @names, /scope, and /cd paths."""

    def __init__(self, commands: tuple[str, ...] | list[str] = REPL_COMMANDS,
                 mentions: list[str] | None = None,
                 scope_names: list[str] | None = None) -> None:
        self.commands = list(commands)
        self.mentions = list(mentions or [])
        self.scope_names = list(scope_names or [])
        self._matches: list[str] = []

    def __call__(self, text: str, state: int) -> str | None:
        if state == 0:
            line = ""
            try:
                import readline
                line = readline.get_line_buffer()
            except Exception:  # noqa: BLE001
                line = text
            self._matches = self.matches(text, line)
        try:
            return self._matches[state]
        except IndexError:
            return None

    def matches(self, text: str, line: str | None = None) -> list[str]:
        buf = text if line is None else line
        head = _completion_head(buf)
        if head == "/cd":
            return _cd_path_matches(text, buf)
        if head == "/scope" and not text.startswith("/"):
            words = [*SCOPE_WORDS, *self.scope_names]
            return [word for word in words if word.startswith(text)]
        if text.startswith("@") or (not text and not buf.strip()):
            names = [name for name in self.mentions if name.startswith(text)]
            if text.startswith("@"):
                return names
            return [word for word in self.commands if word.startswith(text)] + names
        if not text or text.startswith("/"):
            return [word for word in self.commands if word.startswith(text)]
        return []


def _completion_head(line: str) -> str:
    token = line.lstrip().split(maxsplit=1)
    return token[0] if token else ""


def _cd_argument(line: str) -> str:
    """Path token after `/cd`, including a trailing slash."""
    parts = line.lstrip().split(maxsplit=1)
    if not parts or parts[0] != "/cd":
        return ""
    return parts[1] if len(parts) > 1 else ""


def _cd_path_matches(text: str, line: str) -> list[str]:
    """Complete `/cd` using the full path, even if readline split on `/`."""
    path_text = _cd_argument(line) or text
    matches = _path_matches(path_text)
    if text == path_text:
        return matches
    if path_text.endswith(text):
        cut = len(path_text) - len(text)
        return [match[cut:] for match in matches]
    return matches


def _path_matches(text: str) -> list[str]:
    expanded = os.path.expanduser(text)
    if text.endswith(("/", "\\")):
        directory, prefix = expanded, ""
    else:
        directory, prefix = os.path.split(expanded)
    directory = directory or "."
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    matches: list[str] = []
    for name in sorted(names):
        if prefix and not name.startswith(prefix):
            continue
        if name.startswith(".") and not prefix.startswith("."):
            continue
        full = os.path.join(directory, name) if directory != "." else name
        if text.startswith("~"):
            home = os.path.expanduser("~")
            if full == home or full.startswith(home + os.sep):
                full = "~" + full[len(home):]
        if os.path.isdir(os.path.join(directory, name)):
            full = full.rstrip("/\\") + "/"
        matches.append(full)
    return matches


def setup_readline(path, *, commands=REPL_COMMANDS, mentions=(),
                   scope_names=()) -> Completer:
    completer = Completer(commands, list(mentions), list(scope_names))
    try:
        import readline
    except ImportError:
        return completer
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(path)
    except OSError:
        pass
    readline.set_history_length(500)
    readline.set_completer(completer)
    doc = getattr(readline, "__doc__", "") or ""
    if "libedit" in doc:
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    # libedit treats / ~ - as word breaks, so /cd docs/src and @name die
    # after the first segment. Keep those inside one completion token.
    delims = readline.get_completer_delims()
    for ch in "@/~-":
        delims = delims.replace(ch, "")
    readline.set_completer_delims(delims)

    def _save() -> None:
        try:
            readline.write_history_file(path)
        except OSError:
            pass

    import atexit
    atexit.register(_save)
    return completer
