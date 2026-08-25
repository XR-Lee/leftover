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
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + " ..."


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

    def _write_event(self, kind: str, text: str) -> None:
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
