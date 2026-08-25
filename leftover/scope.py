"""leftover's skill in other CLIs — on or off, per vendor.

The influence is the SKILL.md leftover drops into each official CLI's skill
directory. On: that CLI asks leftover where work should go. Off: leftover is
gone from that CLI and it works on its own.

Disk is the source of truth. `leftover scope` is the switch. `install-skills`
turns every home on.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import BUILTIN_AGENTS
from . import ui

_REL = {
    "claude": Path(".claude") / "skills" / "leftover" / "SKILL.md",
    "gpt": Path(".codex") / "skills" / "leftover" / "SKILL.md",
    "grok": Path(".grok") / "skills" / "leftover" / "SKILL.md",
    "cursor": Path(".cursor") / "skills" / "leftover" / "SKILL.md",
    "antigravity": Path(".agents") / "skills" / "leftover" / "SKILL.md",
}

HELP = """\
usage: leftover scope [on|off] [name ...]

Turn leftover's skill in other CLIs on or off.
On:  that CLI asks leftover where work should go.
Off: that CLI works on its own.

  leftover scope              switches (TTY) or a table
  leftover scope on grok      add leftover to Grok
  leftover scope off claude   remove leftover from Claude
  leftover scope on           every CLI
  leftover scope off          every CLI
  leftover scope --json
"""


@dataclass(frozen=True)
class SkillHome:
    key: str
    label: str
    aliases: tuple[str, ...]
    rel: Path

    def matches(self, token: str) -> bool:
        token = token.lower().lstrip("@")
        return token == self.key or token in {a.lower() for a in self.aliases}

    def path(self, home: Path | None = None) -> Path:
        return (Path.home() if home is None else home) / self.rel


@dataclass(frozen=True)
class Row:
    key: str
    label: str
    path: Path
    on: bool


@dataclass
class Cursor:
    index: int = 0


def skill_source() -> Path:
    return Path(__file__).resolve().parent / "skills" / "leftover" / "SKILL.md"


def skill_homes() -> tuple[SkillHome, ...]:
    rows: list[SkillHome] = []
    for raw in BUILTIN_AGENTS:
        key = str(raw["key"])
        rel = _REL.get(key)
        if rel is None:
            continue
        rows.append(SkillHome(
            key=key,
            label=str(raw.get("label", key)),
            aliases=tuple(str(a) for a in raw.get("aliases", ())),
            rel=rel,
        ))
    return tuple(rows)


def skill_destinations(home: Path | None = None) -> list[Path]:
    return [item.path(home) for item in skill_homes()]


def resolve(token: str) -> str | None:
    token = token.lower().lstrip("@")
    if token in {"agents"}:
        return "antigravity"
    for item in skill_homes():
        if item.matches(token):
            return item.key
    return None


def is_linked(dest: Path) -> bool:
    try:
        return dest.is_symlink() or dest.exists()
    except OSError:
        return False


def link_skill(src: Path, dest: Path) -> Path:
    """Point dest at src. Replace a copied file so later edits stay in sync."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = src.resolve()
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    dest.symlink_to(target)
    return dest


def unlink_skill(dest: Path) -> Path:
    """Remove leftover's skill file. Leave every other skill in that CLI."""
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    leftover_dir = dest.parent
    if leftover_dir.is_dir() and leftover_dir.name == "leftover":
        with contextlib.suppress(OSError):
            leftover_dir.rmdir()
    return dest


def snapshot(home: Path | None = None) -> list[Row]:
    return [
        Row(item.key, item.label, item.path(home), is_linked(item.path(home)))
        for item in skill_homes()
    ]


def payload(home: Path | None = None) -> dict:
    return {
        "homes": [
            {"key": row.key, "label": row.label,
             "path": str(row.path), "on": row.on}
            for row in snapshot(home=home)
        ]
    }


def apply(on: bool, keys: list[str], *, home: Path | None = None,
          src: Path | None = None) -> list[Row]:
    src = skill_source() if src is None else src
    if on and not src.is_file():
        raise SystemExit(f"skill file missing: {src}")
    wanted = set(keys)
    for item in skill_homes():
        if item.key not in wanted:
            continue
        dest = item.path(home)
        if on:
            link_skill(src, dest)
        else:
            unlink_skill(dest)
    return snapshot(home=home)


def install_all(home: Path | None = None, src: Path | None = None) -> str:
    src = skill_source() if src is None else src
    if not src.is_file():
        return f"skill file missing: {src}"
    written = [
        str(link_skill(src, item.path(home)))
        for item in skill_homes()
    ]
    return "linked:\n" + "\n".join(f"  {p}" for p in written)


def _display(path: Path, home: Path | None = None) -> str:
    root = Path.home() if home is None else home
    folder = path.parent
    try:
        return "~/" + folder.relative_to(root).as_posix()
    except ValueError:
        return str(folder)


def format_table(home: Path | None = None) -> str:
    rows = snapshot(home=home)
    width = max((len(row.label) for row in rows), default=10)
    lines = ["leftover skill scope"]
    for row in rows:
        mark = "on " if row.on else "off"
        painted = ui.ok(mark) if row.on else ui.dim(mark)
        lines.append(
            f"  {painted}  {row.label:<{width}}  {_display(row.path, home)}")
    return "\n".join(lines)


def doctor_line(home: Path | None = None) -> str:
    rows = snapshot(home=home)
    on = [row.label for row in rows if row.on]
    off = [row.label for row in rows if not row.on]
    if on and not off:
        return "  skill: " + " · ".join(on)
    if not on:
        return ui.dim("  skill: off")
    return ("  skill: " + " · ".join(on)
            + ui.dim("  off: " + " · ".join(off)))


def apply_key(key: str, cursor: Cursor, *, home: Path | None = None,
              src: Path | None = None) -> bool:
    """Handle one TUI key. True = keep looping. Disk updates immediately."""
    rows = snapshot(home=home)
    n = len(rows)
    if not n or key in ("q", "Q", "\x03", "\x04"):
        return False
    if key in ("k", "\x1b[A"):
        cursor.index = (cursor.index - 1) % n
        return True
    if key in ("j", "\x1b[B"):
        cursor.index = (cursor.index + 1) % n
        return True
    if key in ("a", "A"):
        apply(True, [row.key for row in rows], home=home, src=src)
        return True
    if key in ("n", "N"):
        apply(False, [row.key for row in rows], home=home, src=src)
        return True
    if key in (" ", "\r", "\n"):
        row = rows[cursor.index]
        apply(not row.on, [row.key], home=home, src=src)
        return True
    if len(key) == 1 and key.isdigit():
        index = int(key) - 1
        if 0 <= index < n:
            cursor.index = index
            row = rows[index]
            apply(not row.on, [row.key], home=home, src=src)
    return True


def render_panel(cursor: int, home: Path | None = None) -> str:
    rows = snapshot(home=home)
    width = max((len(row.label) for row in rows), default=10)
    lines = [
        ui.bold("leftover") + ui.dim("  ·  skill scope"),
        ui.dim("space toggle   j/k move   a all   n none   q done"),
        "",
    ]
    for i, row in enumerate(rows):
        pointer = ui.bold("›") if i == cursor else " "
        mark = ui.ok("[x]") if row.on else ui.dim("[ ]")
        lines.append(
            f"  {pointer} {mark}  {row.label:<{width}}  "
            f"{ui.dim(_display(row.path, home))}")
    lines.extend([
        "",
        ui.dim("on   leftover answers from this CLI"),
        ui.dim("off  this CLI works on its own"),
    ])
    return "\n".join(lines)


def _read_key(fd: int) -> str:
    import select
    import termios
    import tty
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        buf = os.read(fd, 1)
        if buf == b"\x1b" and select.select([fd], [], [], 0.05)[0]:
            buf += os.read(fd, 8)
        return buf.decode("latin1")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def panel(*, home: Path | None = None, src: Path | None = None) -> None:
    import termios
    cursor = Cursor()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()
    try:
        while True:
            sys.stdout.write("\033[H\033[J")
            sys.stdout.write(render_panel(cursor.index, home=home))
            sys.stdout.write("\n")
            sys.stdout.flush()
            if not apply_key(_read_key(fd), cursor, home=home, src=src):
                return
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _parse_action(tokens: list[str]) -> tuple[bool | None, list[str]]:
    if not tokens:
        return None, []
    verb = tokens[0].lower()
    if verb in ("on", "enable", "add"):
        return True, tokens[1:]
    if verb in ("off", "disable", "remove", "rm"):
        return False, tokens[1:]
    raise SystemExit("leftover scope: expected on|off [name...]")


def _resolve_names(names: list[str]) -> list[str]:
    homes = skill_homes()
    if not names:
        return [item.key for item in homes]
    keys: list[str] = []
    unknown: list[str] = []
    for name in names:
        key = resolve(name)
        if key is None:
            unknown.append(name)
        elif key not in keys:
            keys.append(key)
    if unknown:
        known = ", ".join(item.key for item in homes)
        raise SystemExit(
            f"leftover scope: unknown {', '.join(unknown)} (try {known})")
    return keys


def dispatch(tokens: list[str], *, as_json: bool = False,
             home: Path | None = None, src: Path | None = None,
             interactive: bool | None = None) -> str:
    if tokens and tokens[0] in ("help", "-h", "--help"):
        return HELP.strip()
    on, names = _parse_action(tokens)
    if on is None:
        if as_json:
            return json.dumps(payload(home=home), indent=2, ensure_ascii=False)
        if interactive is None:
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if interactive:
            try:
                panel(home=home, src=src)
            except (OSError, ImportError):
                return format_table(home=home)
            return ""
        return format_table(home=home)
    apply(on, _resolve_names(names), home=home, src=src)
    if as_json:
        return json.dumps(payload(home=home), indent=2, ensure_ascii=False)
    return format_table(home=home)


def run(tokens: list[str], *, as_json: bool = False,
        home: Path | None = None) -> int:
    try:
        text = dispatch(tokens, as_json=as_json, home=home)
    except SystemExit as exc:
        if isinstance(exc.code, int) or exc.code is None:
            return int(exc.code or 0)
        sys.stderr.write(str(exc.code).rstrip() + "\n")
        return 2
    if text:
        sys.stdout.write(text.rstrip() + "\n")
        sys.stdout.flush()
    return 0
