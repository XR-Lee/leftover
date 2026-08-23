#!/usr/bin/env python3
"""Render `docs/demo.svg` — the terminal demo in the README.

Every frame below is real output from `leftover doctor`, `leftover --why` and
`leftover quota` on a machine with all four CLIs logged in. The only edits are
account identity (a display name and an email became `you`), because the
recording is published.

Re-render after changing a frame:

    python scripts/demo_cast.py

The result is a self-contained SVG: CSS keyframes only, no script, no fonts to
fetch, so GitHub renders and animates it inline.
"""
from __future__ import annotations

from pathlib import Path

CAPTURED_AT = "2026-08-23"

DOCTOR = """\
leftover doctor
  Claude     2.1.241          remaining ██████░░░░  55%  weekly
  Codex      codex-cli 0.149. remaining ██████░░░░  59%  weekly
  Grok       grok 1.0.5       remaining ███░░░░░░░  30%  weekly
  Cursor     2026.08.11-e8db8 remaining ██░░░░░░░░  15%  monthly
  on: Claude · Codex · Grok · Cursor
  config: ~/.config/leftover/leftover.toml
  state:  ~/.local/share/leftover/leftover-state.json"""

WHY = """\
task: coding  axis: lag+waste

  agent       lag   waste   total  remaining         window
  grok       0.21   0.015   0.121  ███░░░░░░░  30%   weekly 70% · 14.6h left  ← launching
  cursor     0.07   0.000   0.037  █████████░  87%   monthly auto 13% · 590.8h left
  gpt        0.00   0.000   0.000  ██████░░░░  59%   weekly 41% · 99.3h left
  claude        —       —       —  —                 (not scored)

→ Grok  (weekly 70% used, 14.6h left, lag 0.21 waste 0.015 (reported))"""

QUOTA = """\
usage rhythm  ·  23 Aug 2026 02:11 BST
▾behind / ▴ahead = vs calendar  ·  ↑ same-window increase  ·  new window from 0

Claude · you  ·  7d ▾behind  ·  ↑1% · narrowing
7d 46% vs calendar 95.3% · resets in 7.8h · resets 23 Aug 10:00
calendar ███████████████░ 95.3%
used     ███████░░░░░░░░░ 46%

official weekly pool · SuperGrok Heavy · ▾behind
used 70% · left 30% · calendar 91.3% · resets in 14.6h
calendar ███████████████░ 91.3%
used     ███████████░░░░░ 70%

strategy: lag_waste  order: claude, gpt, cursor, grok"""

SCREENS = [
    ("leftover doctor", DOCTOR, 3.4),
    ('leftover --why "migrate sessions onto JWT"', WHY, 5.2),
    ("leftover quota", QUOTA, 5.4),
]

# Layout
FONT = ("ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, "
        "'Liberation Mono', monospace")
FONT_SIZE = 14.5
CHAR_W = 8.7
LINE_H = 21.0
PAD_X = 22.0
PAD_TOP = 46.0
PAD_BOTTOM = 18.0
TYPE_PAUSE = 0.75      # command on screen before its output lands
LINE_STEP = 0.055      # output lines land this far apart

# Palette (one dark card; readable on a light or dark README)
BG = "#11141a"
CHROME = "#1a1e27"
STROKE = "#262c38"
FG = "#d5dae4"
DIM = "#6d7686"
GREEN = "#7ee2a8"
AMBER = "#f2c078"


def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def runs(line: str) -> list[tuple[str, str | None]]:
    """Split one line into (text, colour) runs. Bars and marks get colour."""
    if line.startswith("$ "):
        return [("$ ", GREEN), (line[2:], FG)]
    if line.startswith("→"):
        return [(line, AMBER)]
    if line.startswith("▾behind") or line.startswith("strategy:"):
        return [(line, DIM)]
    out: list[tuple[str, str | None]] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            out.append((buf, None))
            buf = ""

    head, mark, tail = line.partition("← launching")
    for ch in head:
        if ch == "█":
            flush()
            out.append((ch, GREEN))
        elif ch == "░":
            flush()
            out.append((ch, DIM))
        else:
            buf += ch
    flush()
    if mark:
        out.append((mark + tail, AMBER))
    return out


def build() -> str:
    screens: list[list[tuple[str, float]]] = []
    clock = 0.0
    for command, body, dwell in SCREENS:
        lines: list[tuple[str, float]] = [(f"$ {command}", clock)]
        clock += TYPE_PAUSE
        for line in body.split("\n"):
            lines.append((line, clock))
            clock += LINE_STEP
        clock += dwell
        screens.append(lines)
    total = round(clock, 2)

    rows = max(len(lines) for lines in screens) + 1
    cols = max(len(line) for lines in screens for line, _ in lines)
    width = round(cols * CHAR_W + PAD_X * 2)
    height = round(rows * LINE_H + PAD_TOP + PAD_BOTTOM)

    css: list[str] = [
        f".t{{font-family:{FONT};font-size:{FONT_SIZE}px;"
        f"fill:{FG};white-space:pre}}",
        f".dim{{fill:{DIM}}}",
        ".cur{animation:blink 1.06s steps(1) infinite}",
        "@keyframes blink{0%{opacity:1}50%{opacity:0}}",
    ]
    body: list[str] = []
    index = 0
    for screen, lines in enumerate(screens):
        end = (screens[screen + 1][0][1] if screen + 1 < len(screens)
               else total)
        for row, (line, at) in enumerate(lines):
            index += 1
            a = round(at / total * 100, 3)
            b = round(end / total * 100, 3)
            stops = ["0%{opacity:0}", f"{a}%{{opacity:1}}"]
            if b < 100:
                stops.append(f"{b}%{{opacity:0}}")
            css.append("@keyframes k%d{%s}" % (index, "".join(stops)))
            css.append(
                ".l%d{opacity:0;animation:k%d %ss infinite step-end}"
                % (index, index, total))
            y = PAD_TOP + (row + 1) * LINE_H
            spans = []
            x = PAD_X
            for text, colour in runs(line):
                fill = f' fill="{colour}"' if colour else ""
                spans.append(
                    f'<tspan x="{round(x, 1)}" y="{round(y, 1)}"{fill}>'
                    f"{esc(text)}</tspan>")
                x += len(text) * CHAR_W
            body.append(
                f'<text class="t l{index}">{"".join(spans)}</text>')
            if row == 0:                      # cursor parks after the command
                cx = PAD_X + len(line) * CHAR_W + 2
                cy = y - FONT_SIZE + 2
                index += 1
                gone = round((at + TYPE_PAUSE) / total * 100, 3)
                css.append(
                    "@keyframes k%d{0%%{opacity:0}%s%%{opacity:1}"
                    "%s%%{opacity:0}}" % (index, a, gone))
                css.append(
                    ".l%d{opacity:0;animation:k%d %ss infinite step-end}"
                    % (index, index, total))
                body.append(
                    f'<rect class="l{index} cur" x="{round(cx, 1)}" '
                    f'y="{round(cy, 1)}" width="{round(CHAR_W, 1)}" '
                    f'height="{round(FONT_SIZE + 3, 1)}" fill="{GREEN}" '
                    'opacity="0"/>')

    dots = "".join(
        f'<circle cx="{22 + i * 19}" cy="24" r="6" fill="{c}"/>'
        for i, c in enumerate(("#e06c75", "#e5c07b", "#98c379")))
    title = (f'<text x="{width / 2}" y="29" text-anchor="middle" class="t dim"'
             f' font-size="12.5">leftover</text>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" \
height="{height}" viewBox="0 0 {width} {height}" role="img" \
aria-label="leftover terminal demo: doctor, why, quota">
<style>{"".join(css)}</style>
<rect width="{width}" height="{height}" rx="10" fill="{BG}" \
stroke="{STROKE}"/>
<rect width="{width}" height="46" rx="10" fill="{CHROME}"/>
<rect y="36" width="{width}" height="10" fill="{CHROME}"/>
<line x1="0" y1="46" x2="{width}" y2="46" stroke="{STROKE}"/>
{dots}
{title}
{chr(10).join(body)}
</svg>
"""


def main() -> int:
    out = Path(__file__).resolve().parent.parent / "docs" / "demo.svg"
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
