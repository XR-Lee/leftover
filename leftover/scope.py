"""leftover's skill in other CLIs — on or off, per vendor.

The influence is the SKILL.md leftover drops into each official CLI's skill
directory. On: that CLI asks leftover where work should go. Off: the live pick
gate keeps work in that CLI even if a cached or cross-discovered skill remains.

Disk is the source of truth. The canonical ``skills/leftover`` link records
the requested state for each caller. Vendor CLIs can cache skills and scan one
another's compatibility directories, so ``leftover --pick --agent`` also
checks that link at runtime. ``install-skills`` turns every home on.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
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

# Before the public rename, installs used ``skills/macbot`` while the skill
# body already identified itself as leftover. Those links remain discoverable
# after the canonical link is removed, so every toggle also migrates them.
_LEGACY_REL = {
    key: rel.parent.parent / "macbot" / "SKILL.md"
    for key, rel in _REL.items()
}

_CONFIG_HOME_ENV = {
    "claude": "CLAUDE_CONFIG_DIR",
    "gpt": "CODEX_HOME",
    "grok": "GROK_HOME",
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
    legacy_rel: Path
    config_home_env: str = ""

    def matches(self, token: str) -> bool:
        token = token.lower().lstrip("@")
        return token == self.key or token in {a.lower() for a in self.aliases}

    def path(self, home: Path | None = None) -> Path:
        return self._path(self.rel, home)

    def legacy_path(self, home: Path | None = None) -> Path:
        return self._path(self.legacy_rel, home)

    def _path(self, rel: Path, home: Path | None) -> Path:
        if home is None and self.config_home_env:
            configured = os.environ.get(self.config_home_env, "").strip()
            if configured:
                root = Path(os.path.expandvars(configured)).expanduser()
                return root / Path(*rel.parts[1:])
        return (Path.home() if home is None else home) / rel


@dataclass(frozen=True)
class Row:
    key: str
    label: str
    path: Path
    on: bool
    legacy_paths: tuple[Path, ...] = ()


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
            legacy_rel=_LEGACY_REL[key],
            config_home_env=_CONFIG_HOME_ENV.get(key, ""),
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


def _looks_like_leftover_skill(text: str) -> bool:
    head = text[:4096]
    return ("name: leftover" in head
            and ("leftover --pick" in text or "macbot --pick" in text))


def is_owned_legacy(dest: Path, src: Path | None = None) -> bool:
    """Return whether an old ``skills/macbot`` entry belongs to leftover."""
    src = skill_source() if src is None else src
    try:
        if dest.is_symlink():
            target = dest.resolve(strict=False)
            if target == src.resolve(strict=False):
                return True
            if target.is_file():
                return _looks_like_leftover_skill(
                    target.read_text(errors="replace"))
            # Old editable installs can leave a broken link after the checkout
            # moves. The package-shaped target is specific enough to migrate.
            return target.as_posix().endswith(
                "/leftover/skills/leftover/SKILL.md")
        if not dest.is_file():
            return False
        text = dest.read_text(errors="replace")
    except OSError:
        return False
    return _looks_like_leftover_skill(text)


def link_skill(src: Path, dest: Path) -> Path:
    """Point dest at src. Replace a copied file so later edits stay in sync."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = src.resolve()
    fd, tmp_name = tempfile.mkstemp(
        prefix=".leftover-skill-", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.unlink()
        tmp.symlink_to(target)
        os.replace(tmp, dest)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    return dest


def unlink_skill(dest: Path) -> Path:
    """Remove leftover's skill file. Leave every other skill in that CLI."""
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    leftover_dir = dest.parent
    if leftover_dir.is_dir() and leftover_dir.name in {"leftover", "macbot"}:
        with contextlib.suppress(OSError):
            leftover_dir.rmdir()
    return dest


def unlink_legacy_skill(dest: Path, src: Path | None = None) -> bool:
    """Remove only a legacy macbot entry that is recognizably leftover's."""
    if not is_owned_legacy(dest, src=src):
        return False
    unlink_skill(dest)
    return True


def snapshot(home: Path | None = None, src: Path | None = None) -> list[Row]:
    rows: list[Row] = []
    for item in skill_homes():
        path = item.path(home)
        legacy = item.legacy_path(home)
        legacy_paths = (legacy,) if is_owned_legacy(legacy, src=src) else ()
        rows.append(Row(
            item.key, item.label, path, is_linked(path), legacy_paths))
    return rows


def status(token: str, home: Path | None = None,
           src: Path | None = None) -> Row | None:
    """Return the canonical requested state for one calling CLI."""
    key = resolve(token)
    if key is None:
        return None
    return next(
        (row for row in snapshot(home=home, src=src) if row.key == key), None)


def payload(home: Path | None = None, src: Path | None = None) -> dict:
    return {
        "homes": [
            {"key": row.key, "label": row.label,
             "path": str(row.path), "on": row.on,
             "legacy_paths": [str(path) for path in row.legacy_paths]}
            for row in snapshot(home=home, src=src)
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
        unlink_legacy_skill(item.legacy_path(home), src=src)
        if on:
            link_skill(src, dest)
        else:
            unlink_skill(dest)
    return snapshot(home=home, src=src)


def reconcile(home: Path | None = None, src: Path | None = None) -> list[Row]:
    """If the canonical leftover skill is off, remove leftover-owned macbot paths.

    leftover scope off used to unlink only ``skills/leftover``. Pre-rename
    ``skills/macbot`` links stayed discoverable, so a CLI still loaded leftover
    while leftover scope reported off. leftover scope listing and leftover
    --pick finish that cleanup.
    """
    src = skill_source() if src is None else src
    for item in skill_homes():
        if not is_linked(item.path(home)):
            unlink_legacy_skill(item.legacy_path(home), src=src)
    return snapshot(home=home, src=src)


def install_all(home: Path | None = None, src: Path | None = None) -> str:
    src = skill_source() if src is None else src
    if not src.is_file():
        return f"skill file missing: {src}"
    written: list[str] = []
    for item in skill_homes():
        unlink_legacy_skill(item.legacy_path(home), src=src)
        written.append(str(link_skill(src, item.path(home))))
    return "linked:\n" + "\n".join(f"  {p}" for p in written)


def _display(path: Path, home: Path | None = None) -> str:
    root = Path.home() if home is None else home
    folder = path.parent
    try:
        return "~/" + folder.relative_to(root).as_posix()
    except ValueError:
        return str(folder)


def format_table(home: Path | None = None, src: Path | None = None) -> str:
    rows = snapshot(home=home, src=src)
    width = max((len(row.label) for row in rows), default=10)
    lines = ["leftover skill scope"]
    for row in rows:
        mark = "on " if row.on else "off"
        painted = ui.ok(mark) if row.on else ui.dim(mark)
        legacy = ui.err("  legacy cleanup pending") if row.legacy_paths else ""
        lines.append(
            f"  {painted}  {row.label:<{width}}  {_display(row.path, home)}"
            f"{legacy}")
    return "\n".join(lines)


def doctor_line(home: Path | None = None, src: Path | None = None) -> str:
    rows = snapshot(home=home, src=src)
    on = [row.label for row in rows if row.on]
    off = [row.label for row in rows if not row.on]
    legacy = [row.label for row in rows if row.legacy_paths]
    if on and not off:
        line = "  skill: " + " · ".join(on)
    elif not on:
        line = ui.dim("  skill: off")
    else:
        line = ("  skill: " + " · ".join(on)
                + ui.dim("  off: " + " · ".join(off)))
    if legacy:
        line += ui.err("  legacy: " + " · ".join(legacy))
    return line


def apply_key(key: str, cursor: Cursor, *, home: Path | None = None,
              src: Path | None = None) -> bool:
    """Handle one TUI key. True = keep looping. Disk updates immediately."""
    rows = snapshot(home=home, src=src)
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


def render_panel(cursor: int, home: Path | None = None,
                 src: Path | None = None) -> str:
    rows = snapshot(home=home, src=src)
    width = max((len(row.label) for row in rows), default=10)
    lines = [
        ui.bold("leftover") + ui.dim("  ·  skill scope"),
        ui.dim("space toggle   j/k move   a all   n none   q done"),
        "",
    ]
    for i, row in enumerate(rows):
        pointer = ui.bold("›") if i == cursor else " "
        mark = ui.ok("[x]") if row.on else ui.dim("[ ]")
        legacy = ui.err("  legacy") if row.legacy_paths else ""
        lines.append(
            f"  {pointer} {mark}  {row.label:<{width}}  "
            f"{ui.dim(_display(row.path, home))}{legacy}")
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
            sys.stdout.write(render_panel(cursor.index, home=home, src=src))
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
        reconcile(home=home, src=src)
        if as_json:
            return json.dumps(
                payload(home=home, src=src), indent=2, ensure_ascii=False)
        if interactive is None:
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if interactive:
            try:
                panel(home=home, src=src)
            except (OSError, ImportError):
                return format_table(home=home, src=src)
            return ""
        return format_table(home=home, src=src)
    apply(on, _resolve_names(names), home=home, src=src)
    if as_json:
        return json.dumps(
            payload(home=home, src=src), indent=2, ensure_ascii=False)
    return format_table(home=home, src=src)


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
