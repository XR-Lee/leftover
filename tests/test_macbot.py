"""Lag/waste scoring, intent parsing, and MacBot pick chain."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leftover import intent as intent_mod                            # noqa: E402
from leftover.config import (                                       # noqa: E402
    BUILTIN_AGENTS, AgentSpec, Config, Routing)
from leftover import ui as ui_mod                                    # noqa: E402
from leftover.macbot import (                                        # noqa: E402
    Pick, apply_use, decide, format_why, load_state, parse_duration,
    prepare_task, run_argv, run_print, run_discuss, save_state,
    why_payload)
from leftover.agents.base import BaseRunner, Event, Turn             # noqa: E402
from leftover.quota import Quota, Window, REPORTED, ESTIMATED        # noqa: E402
from leftover.score import (                                        # noqa: E402
    AgentScore, WindowScore, pick_plan, rank_tuple, score_quota)

RESULTS: list[bool] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  ' + detail if detail else ''}")


def test_compose_followup_is_bare() -> None:
    print("\n[macbot.0] harness compose")
    from leftover.macbot import _compose
    from leftover.transcript import Transcript
    spec = AgentSpec(key="gpt", label="Codex", emoji="G")
    tr = Transcript()
    tr.add("You", "fix auth")
    tr.add("Codex", "done")
    first = _compose(spec, "fix auth", tr, followup=False, kind="coding")
    later = _compose(spec, "add a test", tr, followup=True, kind="coding")
    check("first turn boots the harness", "Do the work now" in first)
    check("spawned worker must not re-enter macbot", "Do not run leftover" in first)
    check("worker speaks to the human, not leftover",
          "leftover is routing, not your reader" in first
          and "top-level assistant" not in first
          and "status report for leftover" in first)
    check("follow-up is just the user line", later == "add a test")
    plan = _compose(spec, "split worker", Transcript(), followup=False, kind="plan")
    check("plan turn forbids edits", "Plan only" in plan)
    check("plan worker must not re-enter macbot", "Do not run leftover" in plan)
    check("plan is for the human, not leftover",
          "Report the plan back to leftover" not in plan
          and "leftover is routing, not your reader" in plan)
    heavy = _compose(spec, "should we split", Transcript(), followup=False,
                     kind="heavy")
    check("heavy first turn is discussion/collab, not dump-and-done",
          "leftover heavy" in heavy and "Do the work now" not in heavy
          and "Do not run leftover" in heavy)
    check("heavy also speaks to the human",
          "leftover is routing, not your reader" in heavy)
    from leftover.orchestrator import _GROUP_FRAME
    check("group frame does not treat depth as license to ramble",
          "needs depth" not in _GROUP_FRAME
          and "leftover is routing, not your reader" in _GROUP_FRAME)


def test_intent() -> None:
    print("\n[macbot.1] intent")
    p = intent_mod.parse("/plan split the worker")
    check(" /plan is plan", p.kind == "plan" and p.prompt == "split the worker")
    c = intent_mod.parse("/cu click through the signup")
    check(" /cu is computer_use", c.kind == "computer_use")
    n = intent_mod.parse("@codex fix auth")
    check(" @codex names gpt", n.named == "codex" and "fix auth" in n.prompt)
    a = intent_mod.parse("@any whatever")
    check(" @any is unnamed", a.named is None)
    d = intent_mod.parse("fix the flaky test")
    check(" bare text is coding", d.kind == "coding")
    rt = intent_mod.parse("/rt is splitting the worker worth it")
    check(" /rt is roundtable", rt.kind == "roundtable" and "splitting" in rt.prompt)
    two = intent_mod.parse("@claude @gpt should we use JWTs")
    check(" two mentions is roundtable",
          two.kind == "roundtable" and two.named_all == ["claude", "gpt"])
    db = intent_mod.parse("/debate ship today")
    check(" /debate", db.kind == "debate")
    cu_long = intent_mod.parse("/computer-use click settings")
    check(" /computer-use has an exact command boundary",
          cu_long.kind == "computer_use" and cu_long.prompt == "click settings")
    planet = intent_mod.parse("/planet x")
    check(" /planet is not /plan",
          planet.kind == "coding" and planet.prompt == "/planet x")
    computerized = intent_mod.parse("/computerized x")
    check(" /computerized is not /computer",
          computerized.kind == "coding"
          and computerized.prompt == "/computerized x")
    hv = intent_mod.parse("/heavy should we split the worker")
    check(" /heavy is heavy",
          hv.kind == "heavy" and hv.prompt == "should we split the worker")
    disc = intent_mod.parse("/discuss 一起写 README")
    check(" /discuss is heavy", disc.kind == "heavy" and "README" in disc.prompt)
    lift = intent_mod.parse("/heavylift x")
    check(" /heavylift is not /heavy",
          lift.kind == "coding" and lift.prompt == "/heavylift x")
    q = intent_mod.parse("should we split the worker?")
    check(" should we / ? is heavy", q.kind == "heavy")
    collab = intent_mod.parse("一起写 auth 文档")
    check(" 一起写 is heavy", collab.kind == "heavy")
    check(" implement-shaped text stays coding",
          intent_mod.parse("fix the flaky test").kind == "coding")
    two_q = intent_mod.parse("@claude @gpt should we use JWTs")
    check(" two mentions still beat a heavy phrase",
          two_q.kind == "roundtable")


def test_repl_completes_commands_and_mentions() -> None:
    print("\n[macbot.1b] REPL tab completes leftover chrome, not a pager")
    agents = [
        AgentSpec(key="gpt", label="Codex", aliases=["codex"]),
        AgentSpec(key="cursor", label="Cursor", aliases=["composer"]),
        AgentSpec(key="claude", label="Claude", enabled=False,
                  aliases=["cc"]),
    ]
    mentions = ui_mod.mention_tokens(agents)
    check("disabled agents stay off the @ list",
          mentions == ["@gpt", "@codex", "@cursor", "@composer"],
          str(mentions))
    completer = ui_mod.Completer(
        ui_mod.REPL_COMMANDS, mentions, ["gpt", "codex", "cursor"])
    check("/p is only /plan",
          completer.matches("/p", "/p") == ["/plan"],
          str(completer.matches("/p", "/p")))
    check("@c lists cursor aliases and @codex, not disabled claude",
          completer.matches("@c", "@c") == ["@codex", "@cursor", "@composer"],
          str(completer.matches("@c", "@c")))
    empty = completer.matches("", "")
    check("empty tab lists commands and @names",
          "/plan" in empty and "/quota" in empty and "@gpt" in empty,
          str(empty[:8]))
    check("plain words are not stolen",
          completer.matches("fix", "fix the tests") == [])
    check("/scope completes verbs and names",
          completer.matches("o", "/scope o") == ["on", "off"]
          and completer.matches("c", "/scope on c") == ["codex", "cursor"],
          str((completer.matches("o", "/scope o"),
               completer.matches("c", "/scope on c"))))
    with tempfile.TemporaryDirectory() as tmp:
        docs = Path(tmp) / "docs"
        nested = docs / "nested"
        deep = nested / "deep"
        deep.mkdir(parents=True)
        (docs / "notes.md").write_text("x\n")
        (Path(tmp) / "readme.md").write_text("x\n")
        here = os.getcwd()
        try:
            os.chdir(tmp)
            paths = completer.matches("d", "/cd d")
            full = completer.matches("docs/", "/cd docs/")
            partial = completer.matches("docs/ne", "/cd docs/ne")
            split = completer.matches("ne", "/cd docs/ne")
            after_slash = completer.matches("", "/cd docs/")
            third = completer.matches("docs/nested/", "/cd docs/nested/")
        finally:
            os.chdir(here)
        check("/cd completes directories with a trailing slash",
              paths == ["docs/"], str(paths))
        check("/cd lists the next path level after a slash",
              full == ["docs/nested/", "docs/notes.md"], str(full))
        check("/cd completes a nested prefix as one token",
              partial == ["docs/nested/"], str(partial))
        check("/cd still completes when readline split on /",
              split == ["nested/"], str(split))
        check("/cd after a trailing slash lists children, not cwd",
              after_slash == ["nested/", "notes.md"], str(after_slash))
        check("/cd completes a third path level",
              third == ["docs/nested/deep/"], str(third))
    bound = ui_mod.setup_readline(Path(tempfile.mkdtemp()) / "history",
                                  mentions=mentions)
    check("setup_readline returns the same completer shape",
          "/plan" in bound.matches("/p", "/p"),
          str(bound.matches("/p", "/p")))
    try:
        import readline
        delims = readline.get_completer_delims()
    except ImportError:
        delims = ""
    if delims:
        check("path characters stay inside a /cd token",
              all(ch not in delims for ch in "@/~-"),
              repr(delims))


def test_roster_is_per_agent_status_not_logos() -> None:
    print("\n[macbot.1c] group roster exposes phase, role, and stable seats")
    grok = AgentSpec(key="grok", label="Grok", emoji="X")
    claude = AgentSpec(key="claude", label="Claude", emoji="C")
    cursor = AgentSpec(key="cursor", label="Cursor", emoji="K")
    buf = io.StringIO()

    async def exercise() -> tuple[str, bool]:
        roster = ui_mod.Roster(
            mode="heavy", out=buf, width=78, heartbeat_seconds=0)
        await roster.begin_phase(
            mode="heavy", title="independent", index=1, total=2,
            seats=[(grok, "leader"), (claude, "worker")], parallel=True)

        grok_events = await roster.sink(grok, "leader")(grok)
        await grok_events(Event(
            "lifecycle", "queued", {"state": "queued"}))
        await grok_events(Event(
            "lifecycle", "preparing", {"state": "preparing"}))
        await grok_events(Event(
            "lifecycle", "running",
            {"state": "running", "turn_id": "turn-grok"}))
        await grok_events(Event("thought", "checking the worker boundary"))
        await grok_events(Event("error", "quota exhausted"))
        await grok_events(Event("done"))
        failed_stayed_failed = any(
            row.key == "grok" and row.state == "failed"
            for row in roster._rows.values())

        cursor_events = await roster.sink(grok, "leader")(cursor)
        await cursor_events(Event(
            "lifecycle", "queued", {"state": "queued"}))
        await cursor_events(Event(
            "lifecycle", "preparing", {"state": "preparing"}))
        await cursor_events(Event(
            "lifecycle", "running", {"state": "running"}))
        await cursor_events(Event("tool", "Read leftover/macbot.py"))
        await cursor_events(Event("done"))
        await roster.finish(
            grok, Turn(agent=cursor, text="replacement answer",
                       tools=["Read leftover/macbot.py"], seconds=4.0),
            "leader")

        claude_events = await roster.sink(claude, "worker")(claude)
        await claude_events(Event("status", "comparing approaches"))
        await claude_events(Event("done"))
        await roster.finish(
            claude, Turn(agent=claude, text="worker answer", seconds=3.0),
            "worker")
        snapshot = "\n".join(roster.lines())
        await roster.end_phase()
        return snapshot, failed_stayed_failed

    text, failed_stayed_failed = asyncio.run(exercise())
    check("roster shows mode, phase, progress, and parallel shape",
          "heavy" in text and "phase 1/2" in text
          and "2/2 finished" in text and "parallel" in text, text)
    check("rows keep badges, roles, current activity, and final metrics",
          "X Grok" in text and "K Cursor" in text and "C Claude" in text
          and "leader" in text and "worker" in text
          and "Read leftover/macbot.py" not in text
          and "1 tool" in text and "4s" in text, text)
    check("fallback keeps the seat and names the replacement",
          "replaced" in text and "continued by Cursor" in text, text)
    check("an error followed by done cannot become success",
          failed_stayed_failed, text)
    log = buf.getvalue()
    check("non-tty logs retain phase, role, fallback, and completion context",
          "heavy · phase 1/2 · independent" in log
          and "leader · replaced · continued by Cursor" in log
          and "2/2 finished" in log and "complete" in log, log)
    check("append-only lifecycle advances without starting-to-queued reversal",
          " · starting · " not in log
          and log.count("Grok · leader · queued") == 1
          and log.count("Cursor · leader · queued") == 1
          and log.find("Grok · leader · queued")
          < log.find("Grok · leader · preparing")
          and log.find("Cursor · leader · queued")
          < log.find("Cursor · leader · preparing"),
          log)

    stopped_buf = io.StringIO()

    async def stop_sequential_phase() -> str:
        roster = ui_mod.Roster(
            mode="roundtable", out=stopped_buf, width=78,
            heartbeat_seconds=0)
        await roster.begin_phase(
            mode="roundtable", title="opening positions", index=1, total=1,
            seats=[
                (grok, "opening"),
                (claude, "response"),
                (cursor, "review"),
            ], parallel=False)
        grok_events = await roster.sink(grok, "opening")(grok)
        await grok_events(Event(
            "lifecycle", "queued", {"state": "queued"}))
        await grok_events(Event(
            "lifecycle", "preparing", {"state": "preparing"}))
        await grok_events(Event(
            "lifecycle", "running", {"state": "running"}))
        await roster.end_phase()
        return stopped_buf.getvalue()

    stopped_log = asyncio.run(stop_sequential_phase())
    check("append-only cancellation retains every seat, role, and stopped state",
          "Grok · opening · stopped" in stopped_log
          and "Claude · response · stopped" in stopped_log
          and "Cursor · review · stopped" in stopped_log
          and "3/3 finished · 3 failed" in stopped_log
          and "sequential · complete" in stopped_log,
          stopped_log)

    narrow = ui_mod.Roster(
        [AgentSpec(key="long", label="VeryLongModelName", emoji="V")],
        title="independent", out=io.StringIO(), width=40,
        heartbeat_seconds=0)
    narrow.mark(
        narrow.ensure(AgentSpec(
            key="long", label="VeryLongModelName", emoji="V")),
        "tool", "A very long tool activity that must not wrap")
    narrow_lines = narrow.lines()
    check("narrow terminal rows stay within the physical width",
          all(ui_mod._display_width(line) <= 40 for line in narrow_lines),
          repr(narrow_lines))

    wide_glyphs = ui_mod.Roster(
        [AgentSpec(key="wide", label="模型", emoji="🤖")],
        title="并行分析", out=io.StringIO(), width=20,
        heartbeat_seconds=0)
    wide_glyphs.mark(
        wide_glyphs.ensure(AgentSpec(
            key="wide", label="模型", emoji="🤖")),
        "tool", "检查实现细节")
    wide_lines = wide_glyphs.lines()
    check("emoji and CJK rows stay within terminal cell width",
          all(ui_mod._display_width(line) <= 20 for line in wide_lines),
          repr([(ui_mod._display_width(line), line) for line in wide_lines]))
    emoji_clusters = ["❤️", "1️⃣", "👨‍👩‍👧‍👦"]
    clipped_family = ui_mod.Roster._clip("👨‍👩‍👧‍👦 family", 5)
    check("VS16, keycap, and ZWJ emoji each occupy one two-cell cluster",
          [ui_mod._display_width(value) for value in emoji_clusters]
          == [2, 2, 2],
          repr([(value, ui_mod._display_width(value))
                for value in emoji_clusters]))
    check("clipping never splits a joined emoji cluster",
          clipped_family == "👨‍👩‍👧‍👦..."
          and ui_mod._display_width(clipped_family) == 5,
          repr(clipped_family))

    class NarrowStderr(io.StringIO):
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 731

    stderr = NarrowStderr()
    original_get_terminal_size = ui_mod.os.get_terminal_size
    original_stdout = ui_mod.sys.stdout
    original_stderr = ui_mod.sys.stderr
    terminal_fds: list[int] = []
    try:
        def fake_terminal_size(fd: int) -> os.terminal_size:
            terminal_fds.append(fd)
            return os.terminal_size((20, 24))

        ui_mod.os.get_terminal_size = fake_terminal_size
        ui_mod.sys.stdout = io.StringIO()
        ui_mod.sys.stderr = stderr
        fd_roster = ui_mod.Roster(
            [AgentSpec(key="stderr", label="NarrowStderrModel", emoji="S")],
            title="independent", heartbeat_seconds=0)
        fd_lines = fd_roster.lines()
    finally:
        ui_mod.os.get_terminal_size = original_get_terminal_size
        ui_mod.sys.stdout = original_stdout
        ui_mod.sys.stderr = original_stderr
    check("roster width follows its stderr fd when stdout is redirected",
          fd_roster.out is stderr and terminal_fds == [731]
          and all(ui_mod._display_width(line) <= 20 for line in fd_lines),
          f"fds={terminal_fds}, lines={fd_lines!r}")


def test_roster_tty_snapshots_cover_terminal_states() -> None:
    print("\n[macbot.1d] group roster snapshots stay width-safe and clean")

    def fake_tty() -> io.StringIO:
        out = io.StringIO()
        out.isatty = lambda: True  # type: ignore[attr-defined]
        return out

    def cursor_moves_match_blocks(raw: str) -> bool:
        cursor = "\033["
        suffix = "A\033[J"
        offset = 0
        found = False
        while True:
            start = raw.find(cursor, offset)
            if start < 0:
                return found
            end = raw.find(suffix, start + len(cursor))
            if end < 0:
                return False
            try:
                move = int(raw[start + len(cursor):end])
            except ValueError:
                return False
            if raw[offset:start].count("\n") != move:
                return False
            offset = end + len(suffix)
            found = True

    async def exercise(width: int) -> tuple[
            list[str], list[str], str, str]:
        grok = AgentSpec(key="grok", label="Grok", emoji="X")
        claude = AgentSpec(key="claude", label="Claude", emoji="C")
        codex = AgentSpec(key="gpt", label="Codex", emoji="G")
        cursor = AgentSpec(key="cursor", label="Cursor", emoji="K")
        out = fake_tty()
        roster = ui_mod.Roster(
            mode="heavy", out=out, width=width,
            heartbeat_seconds=0, close_timeout=0.2)
        await roster.begin_phase(
            mode="heavy", title="independent", index=1, total=2,
            seats=[
                (grok, "leader"),
                (claude, "worker"),
                (codex, "worker"),
            ],
            parallel=True,
        )
        await roster.flush()

        grok_events = await roster.sink(grok, "leader")(grok)
        await grok_events(Event("error", "quota exhausted"))
        await grok_events(Event("done"))
        await roster.flush()
        cursor_events = await roster.sink(grok, "leader")(cursor)
        await cursor_events(Event("tool", "Read leftover/orchestrator.py"))
        await cursor_events(Event("done"))
        await roster.finish(
            grok,
            Turn(agent=cursor, text="replacement answer",
                 tools=["Read leftover/orchestrator.py"], seconds=4.0),
            "leader",
        )

        claude_events = await roster.sink(claude, "worker")(claude)
        await claude_events(Event(
            "lifecycle", "running", {"state": "running"}))
        await roster.finish(
            claude,
            Turn(agent=claude, error="turn timed out", seconds=7.0,
                 meta={"timeout_kind": "turn"}),
            "worker",
        )
        codex_events = await roster.sink(codex, "worker")(codex)
        await codex_events(Event("status", "reviewing the plan"))
        await roster.finish(
            codex,
            Turn(agent=codex, error="stopped", seconds=3.0,
                 meta={"cancelled": True}),
            "worker",
        )
        first_lines = roster.lines()
        await roster.end_phase()
        first_raw = out.getvalue()
        boundary = len(first_raw)

        await roster.begin_phase(
            mode="heavy", title="compare-notes", index=2, total=2,
            seats=[(cursor, "synthesis"), (claude, "discuss")],
            parallel=True,
        )
        await roster.flush()
        cursor_events = await roster.sink(cursor, "synthesis")(cursor)
        await cursor_events(Event("text", "final answer"))
        await cursor_events(Event("done"))
        await roster.finish(
            cursor, Turn(agent=cursor, text="final answer", seconds=2.0),
            "synthesis",
        )
        claude_events = await roster.sink(claude, "discuss")(claude)
        await claude_events(Event("done"))
        await roster.finish(
            claude, Turn(agent=claude, seconds=1.0), "discuss")
        second_lines = roster.lines()
        await roster.end_phase()
        return first_lines, second_lines, first_raw, out.getvalue()[boundary:]

    old_no_color = os.environ.get("NO_COLOR")
    os.environ["NO_COLOR"] = "1"
    try:
        snapshots = {
            width: asyncio.run(exercise(width))
            for width in (20, 40, 80, 120)
        }
    finally:
        if old_no_color is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = old_no_color

    check("20/40/80/120-column snapshots never wrap logical rows",
          all(ui_mod._display_width(line) <= width
              for width, (first, second, _raw1, _raw2) in snapshots.items()
              for line in (*first, *second)),
          repr({
              width: [
                  max(map(ui_mod._display_width, first)),
                  max(map(ui_mod._display_width, second)),
              ]
              for width, (first, second, _raw1, _raw2) in snapshots.items()
          }))
    wide_first = "\n".join(snapshots[120][0])
    wide_second = "\n".join(snapshots[120][1])
    check("fallback, timeout, cancel, and metrics remain distinct",
          "replaced" in wide_first and "continued by Cursor" in wide_first
          and "timeout" in wide_first and "stopped" in wide_first
          and "1 tool" in wide_first,
          wide_first)
    check("the second phase exposes synthesis, discuss, ready, and empty",
          "phase 2/2" in wide_second and "compare-notes" in wide_second
          and "synthesis" in wide_second and "discuss" in wide_second
          and "ready" in wide_second and "empty" in wide_second,
          wide_second)
    raw_phases = [
        raw
        for _width, (_first, _second, raw1, raw2) in snapshots.items()
        for raw in (raw1, raw2)
    ]
    check("NO_COLOR keeps TTY redraws but removes color escapes",
          all("\033[" in raw and "\033[2m" not in raw
              and "\033[0m" not in raw for raw in raw_phases),
          repr(raw_phases[0]))
    check("every cursor-up count matches the prior physical block",
          all(cursor_moves_match_blocks(raw) for raw in raw_phases),
          repr(raw_phases[0]))


def test_score_short_window_beats_fat_monthly() -> None:
    print("\n[macbot.2] a session-only agent still scores on that window")
    now = 1_000_000.0
    codex = Quota(agent="gpt", windows=[Window(
        name="5h", used_percent=20.0, resets_at=now + 1800, source=REPORTED)])
    cursor = Quota(agent="cursor", windows=[Window(
        name="monthly", used_percent=10.0, resets_at=now + 20 * 86400,
        source=ESTIMATED)])
    s_gpt = score_quota("gpt", codex, now=now)
    s_cur = score_quota("cursor", cursor, now=now)
    check("session-only Codex uses 5h as the plan window",
          s_gpt.focus == "5h" and s_gpt.total > s_cur.total,
          f"gpt {s_gpt.total:.3f} vs cursor {s_cur.total:.3f}")
    check("estimated monthly has no waste", s_cur.waste == 0.0)
    check("codex waste is the reason", s_gpt.waste > s_cur.waste)


def test_score_depleted_short_window_loses() -> None:
    print("\n[macbot.3] a spent 5h window loses to a fat monthly")
    now = 1_000_000.0
    codex = Quota(agent="gpt", windows=[Window(
        name="5h", used_percent=96.0, resets_at=now + 1800, source=REPORTED)])
    cursor = Quota(agent="cursor", windows=[Window(
        name="monthly", used_percent=10.0, resets_at=now + 20 * 86400,
        source=ESTIMATED)])
    s_gpt = score_quota("gpt", codex, now=now)
    s_cur = score_quota("cursor", cursor, now=now)
    check("cursor wins when the 5h is empty", s_cur.total > s_gpt.total,
          f"gpt {s_gpt.total:.3f} vs cursor {s_cur.total:.3f}")


def test_score_fresh_short_window_does_not_starve_overdue_weekly() -> None:
    print("\n[macbot.3b] overdue weekly beats a just-reset short window")
    now = 1_000_000.0
    grok_left = 15.3 * 3600
    grok = Quota(agent="grok", windows=[Window(
        name="weekly", used_percent=69.0, resets_at=now + grok_left,
        started_at=now + grok_left - 7 * 86400, source=REPORTED)])
    codex = Quota(agent="gpt", windows=[
        Window(name="5h", used_percent=0.0, resets_at=now + 5 * 3600,
               started_at=now, source=REPORTED),
        Window(name="weekly", used_percent=38.0,
               resets_at=now + 100 * 3600,
               started_at=now + 100 * 3600 - 7 * 86400,
               source=REPORTED),
    ])
    s_grok = score_quota("grok", grok, now=now)
    s_gpt = score_quota("gpt", codex, now=now)
    check("live Grok window outranks fresh Codex",
          s_grok.total > s_gpt.total,
          f"grok {s_grok.total:.3f} vs gpt {s_gpt.total:.3f}")
    check("a just-reset window has no fake catch-up emergency",
          next(w for w in s_gpt.windows if w.name == "5h").waste == 0.0)


async def test_route_respects_ahead_weekly_window() -> None:
    print("\n[macbot.3c] an ahead weekly window gates Codex routing")
    from leftover.router import Router

    now = time.time()
    weekly_reset = now + 163 * 3600
    codex_quota = Quota(agent="gpt", checked_at=now, windows=[
        Window(name="5h", used_percent=0.0, source=REPORTED),
        Window(name="weekly", used_percent=4.0,
               resets_at=weekly_reset,
               started_at=weekly_reset - 7 * 86400,
               source=REPORTED),
    ])
    grok_reset = now + 115 * 3600
    grok_quota = Quota(agent="grok", checked_at=now, windows=[Window(
        name="weekly", used_percent=19.0,
        resets_at=grok_reset,
        started_at=grok_reset - 7 * 86400,
        source=REPORTED,
    )])

    undated = score_quota(
        "gpt", Quota(agent="gpt", windows=[codex_quota.windows[0]]),
        now=now)
    check("an undated reported 5h window has no invented urgency",
          undated.total == 0.0 and undated.windows[0].lag == 0.0
          and "no reset clock" in undated.detail,
          undated.detail)

    gated = score_quota("gpt", Quota(agent="gpt", windows=[
        Window(name="5h", used_percent=0.0,
               resets_at=now + 3600, started_at=now - 4 * 3600,
               source=REPORTED),
        codex_quota.windows[1],
    ]), now=now)
    check("weekly ahead gates even a behind 5h window",
          gated.total == 0.0 and "weekly" in gated.detail
          and "ahead of pace" in gated.detail,
          gated.detail)

    with tempfile.TemporaryDirectory() as tmp:
        gpt = AgentSpec(key="gpt", label="Codex")
        grok = AgentSpec(key="grok", label="Grok")
        cursor = AgentSpec(key="cursor", label="Cursor")
        cfg = Config(
            agents=[gpt, grok, cursor], data_dir=tmp,
            routing=Routing(
                strategy="lag_waste",
                coding_keys=["gpt", "grok", "cursor"]))
        router = Router(cfg, object())
        router.h(gpt).quota = codex_quota
        router.h(gpt).quota_checked = now
        router.h(grok).quota = grok_quota
        router.h(grok).quota_checked = now
        router.h(cursor).quota = Quota(
            agent="cursor", checked_at=now,
            windows=[Window(
                name="monthly", used_percent=0.0, source=REPORTED)])
        router.h(cursor).quota_checked = now
        ranked = await router.rank([gpt, grok, cursor])

    check("behind and neutral windows both route before ahead Codex",
          [spec.key for spec in ranked] == ["grok", "cursor", "gpt"]
          and router.last_scores["gpt"].total == 0.0
          and router.last_scores["gpt"].ahead > 0.0
          and router.last_scores["grok"].total > 0.0,
          repr([spec.key for spec in ranked]))


def test_score_allocation_window_outranks_rotting_session() -> None:
    print("\n[macbot.3e] on-pace weekly + rotting 5h loses to a behind weekly")
    now = 1_000_000.0
    week_left = 84 * 3600
    week_start = now + week_left - 7 * 86400
    grok = Quota(agent="grok", windows=[Window(
        name="weekly", used_percent=20.0, resets_at=now + week_left,
        started_at=week_start, source=REPORTED)])
    codex = Quota(agent="gpt", windows=[
        Window(name="5h", used_percent=0.0, resets_at=now + 1800,
               started_at=now + 1800 - 5 * 3600, source=REPORTED),
        Window(name="weekly", used_percent=50.0, resets_at=now + week_left,
               started_at=week_start, source=REPORTED),
    ])
    cursor = Quota(agent="cursor", windows=[
        Window(name="monthly", used_percent=10.0,
               resets_at=now + 20 * 86400,
               started_at=now + 20 * 86400 - 30 * 86400,
               source=REPORTED),
        Window(name="monthly auto", used_percent=80.0, source=REPORTED),
    ])
    s_grok = score_quota("grok", grok, now=now)
    s_gpt = score_quota("gpt", codex, now=now)
    s_cur = score_quota("cursor", cursor, now=now)
    check("Codex focus stays weekly, not the rotting 5h",
          s_gpt.focus == "weekly" and pick_plan(codex.windows).name == "weekly",
          s_gpt.detail)
    check("behind Grok outranks on-pace Codex even with a dying 5h",
          s_grok.total > s_gpt.total,
          f"grok {s_grok.total:.3f} vs gpt {s_gpt.total:.3f} "
          f"session {s_gpt.session_total:.3f}")
    check("rotting 5h is a tie-break, not total",
          s_gpt.total == 0.0 and s_gpt.session_total > s_grok.total)
    check("Cursor ranks on monthly, not monthly auto",
          s_cur.focus == "monthly", s_cur.detail)
    check("behind monthly beats on-pace weekly + rotting 5h",
          s_cur.total > s_gpt.total,
          f"cursor {s_cur.total:.3f} vs gpt {s_gpt.total:.3f}")
    check("close weeklies let the rotting 5h break the tie",
          rank_tuple(s_gpt) < rank_tuple(AgentScore(
              key="other", lag=s_gpt.lag, waste=s_gpt.waste, total=s_gpt.total,
              source=REPORTED, detail="", windows=s_gpt.windows,
              focus="weekly")))


def test_score_session_ahead_does_not_gate_behind_weekly() -> None:
    print("\n[macbot.3f] a hot 5h does not zero an overdue weekly")
    now = 1_000_000.0
    week_left = 84 * 3600
    week_start = now + week_left - 7 * 86400
    hot = score_quota("gpt", Quota(agent="gpt", windows=[
        Window(name="5h", used_percent=90.0, resets_at=now + 3600,
               started_at=now + 3600 - 5 * 3600, source=REPORTED),
        Window(name="weekly", used_percent=20.0, resets_at=now + week_left,
               started_at=week_start, source=REPORTED),
    ]), now=now)
    check("weekly behind still has urgency",
          hot.total > 0.0 and hot.ahead == 0.0 and hot.focus == "weekly",
          hot.detail)
    full = score_quota("gpt", Quota(agent="gpt", windows=[
        Window(name="5h", used_percent=100.0, resets_at=now + 3600,
               started_at=now + 3600 - 5 * 3600, source=REPORTED),
        Window(name="weekly", used_percent=20.0, resets_at=now + week_left,
               started_at=week_start, source=REPORTED),
    ]), now=now)
    check("a full 5h is skipped without erasing weekly lag",
          full.session_blocked and full.total > 0.0
          and "skip" in full.detail,
          full.detail)
    grok = score_quota("grok", Quota(agent="grok", windows=[Window(
        name="weekly", used_percent=30.0, resets_at=now + week_left,
        started_at=week_start, source=REPORTED)]), now=now)
    check("full 5h ranks after an available behind weekly",
          rank_tuple(grok) < rank_tuple(full)
          and grok.total < full.total)


async def test_route_skips_full_session_window() -> None:
    print("\n[macbot.3g] a full 5h is last even when its weekly is more behind")
    from leftover.router import Router

    now = time.time()
    week_left = 84 * 3600
    week_start = now + week_left - 7 * 86400
    gpt = AgentSpec(key="gpt", label="Codex")
    grok = AgentSpec(key="grok", label="Grok")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            agents=[gpt, grok], data_dir=tmp,
            routing=Routing(strategy="lag_waste",
                            coding_keys=["gpt", "grok"]))
        router = Router(cfg, object())
        router.h(gpt).quota = Quota(agent="gpt", checked_at=now, windows=[
            Window(name="5h", used_percent=100.0, resets_at=now + 3600,
                   started_at=now + 3600 - 5 * 3600, source=REPORTED),
            Window(name="weekly", used_percent=20.0,
                   resets_at=now + week_left, started_at=week_start,
                   source=REPORTED),
        ])
        router.h(gpt).quota_checked = now
        router.h(grok).quota = Quota(agent="grok", checked_at=now, windows=[
            Window(name="weekly", used_percent=30.0,
                   resets_at=now + week_left, started_at=week_start,
                   source=REPORTED),
        ])
        router.h(grok).quota_checked = now
        ranked = await router.rank([gpt, grok])
    check("available Grok beats full-session Codex",
          [spec.key for spec in ranked] == ["grok", "gpt"]
          and router.last_scores["gpt"].session_blocked
          and router.last_scores["gpt"].total
          > router.last_scores["grok"].total,
          repr([spec.key for spec in ranked]))


async def test_sticky_requires_a_live_session() -> None:
    print("\n[macbot.3d] persisted cwd choice cannot override fresh quota ranking")
    from leftover.router import Router

    with tempfile.TemporaryDirectory() as tmp:
        now = time.time()
        gpt = AgentSpec(
            key="gpt", label="Codex", interactive_command=["true"],
            exec_command=["true"], quota_probe="codex")
        grok = AgentSpec(
            key="grok", label="Grok", interactive_command=["true"],
            exec_command=["true"], quota_probe="grok")
        cfg = Config(
            agents=[gpt, grok], data_dir=tmp,
            routing=Routing(strategy="lag_waste", coding_keys=["gpt", "grok"]))
        state = {
            "sticky": {tmp: "gpt"},
            "quota": {
                "gpt": Quota(agent="gpt", checked_at=now, windows=[Window(
                    name="weekly", used_percent=38.0,
                    resets_at=now + 100 * 3600,
                    started_at=now + 100 * 3600 - 7 * 86400,
                    source=REPORTED)]).to_dict(),
                "grok": Quota(agent="grok", checked_at=now, windows=[Window(
                    name="weekly", used_percent=69.0,
                    resets_at=now + 15.3 * 3600,
                    started_at=now + 15.3 * 3600 - 7 * 86400,
                    source=REPORTED)]).to_dict(),
            },
        }
        save_state(cfg, state)
        fresh = await decide(cfg, intent_mod.parse("fix routing"), tmp)
        check("a fresh process re-ranks instead of following disk sticky",
              fresh.spec is not None and fresh.spec.key == "grok"
              and not fresh.sticky, fresh.reason)

        class LiveRunner:
            def live_session(self) -> bool:
                return True

        class LivePool:
            def peek(self, spec: AgentSpec):
                return LiveRunner() if spec.key == "gpt" else None

        live_router = Router(cfg, LivePool())
        unrelated = await decide(
            cfg, intent_mod.parse("new coding task"), tmp, live_router)
        check("an unrelated warm session cannot activate disk sticky",
              unrelated.spec is not None and unrelated.spec.key == "grok"
              and not unrelated.sticky, unrelated.reason)
        live_router.conversation_success = "gpt"
        live = await decide(
            cfg, intent_mod.parse("continue the same change"), tmp,
            live_router)
        check("an actual in-process session keeps conversation continuity",
              live.spec is not None and live.spec.key == "gpt"
              and live.sticky and "live session" in live.reason,
              live.reason)


async def test_pick_plan_and_cu() -> None:
    print("\n[macbot.4] pick respects /plan and /cu")
    with tempfile.TemporaryDirectory() as tmp:
        agents = [
            AgentSpec(key="claude", label="Claude", emoji="C",
                      interactive_command=["true"], exec_command=["true"]),
            AgentSpec(key="gpt", label="Codex", emoji="G",
                      interactive_command=["true"], exec_command=["true"],
                      quota_probe="codex"),
            AgentSpec(key="grok", label="Grok", emoji="X",
                      interactive_command=["true"], exec_command=["true"]),
            AgentSpec(key="cursor", label="Cursor", emoji="K",
                      interactive_command=["true"], exec_command=["true"]),
        ]
        cfg = Config(agents=agents, data_dir=tmp,
                     routing=Routing(strategy="lag_waste",
                                     coding_keys=["gpt", "grok", "cursor"],
                                     plan_key="claude"))
        cu = await decide(cfg, intent_mod.parse("/cu click settings"), tmp)
        check("cu picks Codex CLI",
              cu.kind == "computer_use" and cu.spec is not None
              and cu.spec.key == "gpt", str(cu.chain))
        check("cu announce is the routing entry",
              cu.announce == "leftover · Codex · computer use", cu.announce)
        plan = await decide(cfg, intent_mod.parse("/plan the auth rewrite"), tmp)
        check("plan leads with claude",
              plan.chain[0] == "claude" and plan.spec is not None
              and plan.spec.key == "claude", str(plan.chain))
        check("plan announce hides the chain",
              plan.announce == "leftover · Claude · plan"
              and "→" not in plan.announce and "chain" not in plan.as_dict()["announce"],
              plan.announce)
        named = await decide(cfg, intent_mod.parse("@grok just this"), tmp)
        check("named grok is first", named.chain[0] == "grok", str(named.chain))
        check("named announce is just the entry",
              named.announce == "leftover · Grok", named.announce)
        coding = await decide(cfg, intent_mod.parse("fix tests"), tmp)
        check("coding chain has no claude until the end",
              coding.chain[-1] == "claude" and "claude" not in coding.chain[:-1],
              str(coding.chain))
        check("coding announce is one name",
              coding.announce.startswith("leftover · ")
              and " → " not in coding.announce, coding.announce)
        check("announce helper stays quiet on nobody",
              ui_mod.announce(None) == "leftover · nobody available")
        check("run argv is headless --print --use",
              coding.as_dict()["run"][:4] == ["leftover", "--print", "--use",
                                              coding.spec.key],
              str(coding.as_dict()["run"]))
        completion = coding.as_dict().get("completion") or {}
        check("pick JSON declares process-exit completion without push",
              completion == {
                  "mode": "process_exit",
                  "push": False,
                  "max_poll_interval_seconds": 10,
              }, repr(completion))
        check("plan handoff preserves kind",
              "--plan" in plan.as_dict()["run"],
              str(plan.as_dict()["run"]))
        check("computer-use handoff preserves kind",
              "--cu" in cu.as_dict()["run"], str(cu.as_dict()["run"]))
        unknown = await decide(cfg, intent_mod.parse("@unknown fix tests"), tmp)
        check("unknown @agent does not silently auto-route",
              unknown.spec is None and "unknown agent @unknown" in unknown.reason,
              unknown.reason)
        unknown_plan = await decide(
            cfg, intent_mod.parse("/plan @gpt @missing fix tests"), tmp)
        unknown_cu = await decide(
            cfg, intent_mod.parse("/cu @gpt @missing click settings"), tmp)
        check("plan and computer use reject secondary unknown mentions",
              not unknown_plan.available and not unknown_cu.available
              and "@missing" in unknown_plan.reason
              and "@missing" in unknown_cu.reason)
        empty_plan = await decide(cfg, intent_mod.parse("/plan"), tmp)
        empty_cu = await decide(cfg, intent_mod.parse("/cu"), tmp)
        empty_group = await decide(cfg, intent_mod.parse("/rt"), tmp)
        check("empty routed commands do not advertise executable handoffs",
              all(not pick.available and pick.as_dict()["run"] is None
                  and pick.as_dict()["completion"] is None
                  for pick in (empty_plan, empty_cu, empty_group)),
              "; ".join(pick.reason for pick in
                          (empty_plan, empty_cu, empty_group)))


async def test_pick_heavy_is_local_multi_model_collab() -> None:
    print("\n[macbot.4h] /heavy is local multi-model collab")
    from leftover.macbot import _parse_argv
    with tempfile.TemporaryDirectory() as tmp:
        agents = [
            AgentSpec(key="claude", label="Claude", emoji="C",
                      interactive_command=["true"], exec_command=["true"]),
            AgentSpec(key="gpt", label="Codex", emoji="G",
                      interactive_command=["true"], exec_command=["true"]),
            AgentSpec(key="grok", label="Grok", emoji="X",
                      interactive_command=["true"], exec_command=["true"]),
            AgentSpec(key="cursor", label="Cursor", emoji="K",
                      interactive_command=["true"], exec_command=["true"]),
        ]
        cfg = Config(agents=agents, data_dir=tmp,
                     routing=Routing(strategy="lag_waste",
                                     coding_keys=["gpt", "grok", "cursor"],
                                     plan_key="claude", heavy_key="grok"))
        pick = await decide(cfg, intent_mod.parse("/heavy should we split"), tmp)
        blob = pick.as_dict()
        check("heavy with two+ CLIs is a panel, not one worker",
              pick.kind == "heavy" and pick.spec is None
              and pick.available and pick.chain[0] == "grok"
              and "Claude" in pick.labels and "Grok" in pick.labels,
              repr(pick.chain))
        check("heavy announce is leftover · panel · heavy",
              blob["announce"].startswith("leftover · ")
              and blob["announce"].endswith(" · heavy"),
              blob["announce"])
        check("heavy run is leftover --print /heavy @…",
              blob["run"][:2] == ["leftover", "--print"]
              and blob["run"][2].startswith("/heavy")
              and "@grok" in " ".join(blob["run"]),
              str(blob["run"]))
        check("--heavy stamps the task the same way --plan does",
              intent_mod.parse(prepare_task("should we split", heavy=True)).kind
              == "heavy")
        ns = _parse_argv(["--heavy", "should we split"])
        check("--heavy is a flag, not a route override",
              ns.heavy and ns.prompt == ["should we split"])
        empty = await decide(cfg, intent_mod.parse("/heavy"), tmp)
        check("empty /heavy is not a runnable handoff",
              not empty.available and empty.as_dict()["run"] is None)

        solo = [
            AgentSpec(key="grok", label="Grok", emoji="X",
                      interactive_command=["true"], exec_command=["true"]),
            AgentSpec(key="claude", label="Claude", emoji="C"),
        ]
        one = Config(agents=solo, data_dir=tmp,
                     routing=Routing(heavy_key="grok", plan_key="claude",
                                     coding_keys=["grok"]))
        single = await decide(
            one, intent_mod.parse("/heavy 一起写 README"), tmp)
        check("one installed CLI degrades to a single heavy worker",
              single.spec is not None and single.spec.key == "grok"
              and single.kind == "heavy" and "--heavy" in single.as_dict()["run"],
              str(single.as_dict().get("run")))


def test_why_table_is_lag_waste_not_reputation() -> None:
    print("\n[macbot.4b] --why is usher-shaped, lag+waste axis")
    from leftover.macbot import _parse_argv
    spec = AgentSpec(key="gpt", label="Codex", emoji="G")
    grok = AgentSpec(key="grok", label="Grok", emoji="X")
    scores = {
        "gpt": AgentScore(
            key="gpt", lag=0.05, waste=0.400, total=0.425, source="reported",
            detail="5h 20% used", windows=[WindowScore(
                name="5h", lag=0.05, waste=0.400, total=0.425,
                used_percent=20.0, hours_left=0.5)]),
        "grok": AgentScore(
            key="grok", lag=0.16, waste=0.010, total=0.090, source="reported",
            detail="week 60% used", windows=[WindowScore(
                name="week", lag=0.16, waste=0.010, total=0.090,
                used_percent=60.0, hours_left=40.0)]),
    }
    pick = Pick(spec, ["gpt", "grok"], scores, "5h 20% used, 0.5h left",
                "coding", "fix tests")
    table = format_why(pick)
    check("table names the axis, not task-type reputation",
          "axis: lag+waste" in table and "strength" not in table
          and "task: coding" in table, table)
    check("winner is marked and both windows show",
          "← launching" in table and "5h 20%" in table and "week 60%" in table
          and "→ Codex" in table, table)
    check("remaining bar is remaining, not used",
          "80%" in table and "40%" in table, table)
    ns = _parse_argv(["--why", "fix", "tests"])
    check("--why is a dry-run alias, not a router override",
          ns.why and not ns.headless and not ns.pick
          and ns.prompt == ["fix", "tests"])
    p_ns = _parse_argv(["-p", "fix", "tests"])
    check("-p is usher's print alias",
          p_ns.headless and p_ns.prompt == ["fix", "tests"])

    cfg = Config(agents=[spec, grok], routing=Routing(strategy="lag_waste",
                                                     coding_keys=["gpt", "grok"]))

    async def fake_decide(cfg_arg, parsed, cwd, router=None) -> Pick:
        return pick

    from leftover import macbot as macbot_mod
    original_load = macbot_mod.config_mod.load
    original_decide = macbot_mod.decide
    macbot_mod.config_mod.load = lambda _path: cfg
    macbot_mod.decide = fake_decide
    try:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = macbot_mod.main(["--why", "fix tests"])
        out = stdout.getvalue()
        check("--why prints the table and does not run an agent",
              code == 0 and "axis: lag+waste" in out and "→ Codex" in out
              and stderr.getvalue() == "", repr(out))
        blob = why_payload(pick)
        check("--why JSON keeps the lag+waste columns and no strength",
              blob["axis"] == "lag+waste" and blob["agent"] == "gpt"
              and "strength" not in blob
              and blob["agents"][0]["launching"]
              and blob["agents"][0]["windows"][0]["name"] == "5h"
              and blob["agents"][1]["waste"] == 0.01, repr(blob))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = macbot_mod.main(["--why", "--json", "fix tests"])
        dumped = json.loads(stdout.getvalue())
        check("--why --json prints the table payload, not a pick dump",
              code == 0 and dumped["axis"] == "lag+waste"
              and dumped["agent"] == "gpt" and "run" not in dumped
              and stderr.getvalue() == "", stdout.getvalue())
    finally:
        macbot_mod.decide = original_decide
        macbot_mod.config_mod.load = original_load


def test_agent_is_identity_not_mention() -> None:
    print("\n[macbot.5] --agent is caller identity, --use forces routing")
    from leftover.macbot import _parse_argv
    ns = _parse_argv(["--pick", "--json", "--agent", "cursor",
                      "fix the flaky test"])
    check("--agent is stored", ns.agent == "cursor")
    text = prepare_task(" ".join(ns.prompt))
    check("--agent is not written into the task",
          text == "fix the flaky test" and not text.startswith("@"), text)
    parsed = apply_use(intent_mod.parse(text), ns.use)
    check("identity does not name an agent", parsed.named is None)
    forced = _parse_argv(["--print", "--use", "grok", "fix tests"])
    parsed2 = apply_use(intent_mod.parse(" ".join(forced.prompt)), forced.use)
    check("--use names grok", parsed2.named == "grok" and parsed2.prompt == "fix tests")
    spec = AgentSpec(key="grok", label="Grok", emoji="X")
    check("run_argv pins the chosen backend",
          run_argv(spec, "fix tests") == ["leftover", "--print", "--use", "grok",
                                          "fix tests"])
    tagged = apply_use(intent_mod.parse("@claude rewrite auth"), "grok")
    check("--use wins over an @mention in the text",
          tagged.named == "grok" and tagged.prompt == "rewrite auth")
    forced_plan = prepare_task("/planet x", plan=True)
    check("--plan does not confuse /planet with /plan",
          intent_mod.parse(forced_plan).kind == "plan", forced_plan)
    forced_cu = prepare_task("/cupertino x", cu=True)
    check("--cu does not confuse /cupertino with /cu",
          intent_mod.parse(forced_cu).kind == "computer_use", forced_cu)
    forced_heavy = prepare_task("/heavylift x", heavy=True)
    check("--heavy does not confuse /heavylift with /heavy",
          intent_mod.parse(forced_heavy).kind == "heavy"
          and forced_heavy.startswith("/heavy "), forced_heavy)


def test_cli_routing_progress_is_human_only() -> None:
    print("\n[macbot.5b] CLI routing progress stays off machine outputs")
    from leftover import macbot as macbot_mod

    spec = AgentSpec(key="gpt", label="Codex", emoji="G")
    cfg = Config(
        agents=[spec],
        routing=Routing(strategy="order", order=["gpt"],
                        coding_keys=["gpt"], plan_key="gpt"),
    )

    async def slow_decide(cfg_arg, parsed, cwd, router=None) -> Pick:
        await asyncio.sleep(0.035)
        return Pick(spec, ["gpt"], {}, "test", parsed.kind, parsed.prompt)

    async def fake_run_print(cfg_arg, pick, **_kwargs) -> int:
        sys.stdout.write("FINAL\n")
        return 0

    original_load = macbot_mod.config_mod.load
    original_decide = macbot_mod.decide
    original_run_print = macbot_mod.run_print
    original_heartbeat = macbot_mod.PROGRESS_HEARTBEAT_SECONDS
    from leftover import scope as scope_mod
    original_snapshot = scope_mod.snapshot
    original_reconcile = scope_mod.reconcile
    scope_path = Path("/tmp/codex/skills/leftover/SKILL.md")
    scope_mod.snapshot = lambda **_kw: [
        scope_mod.Row("gpt", "Codex", scope_path, True)]
    scope_mod.reconcile = lambda **_kw: []
    macbot_mod.config_mod.load = lambda _path: cfg
    macbot_mod.decide = slow_decide
    macbot_mod.run_print = fake_run_print
    macbot_mod.PROGRESS_HEARTBEAT_SECONDS = 0.01
    try:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = macbot_mod.main(["--print", "inspect"])
        progress = stderr.getvalue()
        check("ordinary --print reports routing before the answer",
              code == 0 and progress.startswith("leftover: routing...\n")
              and "leftover: still working (routing)" in progress,
              repr(progress))
        check("routing progress does not contaminate --print stdout",
              stdout.getvalue() == "FINAL\n", repr(stdout.getvalue()))

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = macbot_mod.main(["--json", "inspect"])
        try:
            blob = json.loads(stdout.getvalue())
        except json.JSONDecodeError:
            blob = {}
        check("--json remains machine-readable and emits no progress",
              code == 0 and blob.get("agent") == "gpt"
              and stderr.getvalue() == "",
              f"stdout={stdout.getvalue()!r}, stderr={stderr.getvalue()!r}")

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = macbot_mod.main(["--pick", "inspect"])
        try:
            blob = json.loads(stdout.getvalue())
        except json.JSONDecodeError:
            blob = {}
        check("--pick remains machine-readable and emits no progress",
              code == 0 and blob.get("agent") == "gpt"
              and stderr.getvalue() == "",
              f"stdout={stdout.getvalue()!r}, stderr={stderr.getvalue()!r}")
    finally:
        macbot_mod.PROGRESS_HEARTBEAT_SECONDS = original_heartbeat
        macbot_mod.run_print = original_run_print
        macbot_mod.decide = original_decide
        macbot_mod.config_mod.load = original_load
        scope_mod.snapshot = original_snapshot
        scope_mod.reconcile = original_reconcile


async def test_routing_progress_stops_after_cancel() -> None:
    print("\n[macbot.5c] routing heartbeat is cancelled with its task")
    from leftover import macbot as macbot_mod

    cfg = Config(agents=[])
    parsed = intent_mod.parse("inspect")
    original_decide = macbot_mod.decide

    async def blocked_decide(cfg_arg, parsed_arg, cwd, router=None) -> Pick:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    macbot_mod.decide = blocked_decide
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            task = asyncio.create_task(macbot_mod._decide_with_progress(
                cfg, parsed, os.getcwd(), heartbeat_seconds=0.01))
            await asyncio.sleep(0.025)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            stopped = stderr.getvalue()
            await asyncio.sleep(0.025)
        check("cancelled routing leaves no heartbeat behind",
              "leftover: routing..." in stopped
              and "leftover: still working (routing)" in stopped
              and stderr.getvalue() == stopped,
              repr(stderr.getvalue()))

        async def failed_decide(cfg_arg, parsed_arg, cwd, router=None) -> Pick:
            await asyncio.sleep(0.015)
            raise RuntimeError("quota probe failed")

        macbot_mod.decide = failed_decide
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            try:
                await macbot_mod._decide_with_progress(
                    cfg, parsed, os.getcwd(), heartbeat_seconds=0.01)
            except RuntimeError as exc:
                error = str(exc)
            else:
                error = ""
            stopped = stderr.getvalue()
            await asyncio.sleep(0.025)
        check("failed routing leaves no heartbeat behind",
              error == "quota probe failed"
              and "leftover: routing..." in stopped
              and stderr.getvalue() == stopped,
              f"error={error!r}, stderr={stderr.getvalue()!r}")
    finally:
        macbot_mod.decide = original_decide


def test_skill_install_is_symlink() -> None:
    print("\n[macbot.8] install-skills links, does not copy")
    from leftover.scope import link_skill, skill_destinations, skill_source
    dests = [str(path) for path in skill_destinations()]
    check("leftover skill is installed",
          any("/skills/leftover/SKILL.md" in path for path in dests))
    check("the legacy CLI does not duplicate the product skill",
          len(dests) == 5
          and not any("/skills/macbot/SKILL.md" in path for path in dests),
          repr(dests))
    skill = skill_source().read_text()
    check("handoff skill polls the returned process handle promptly",
          "session_id" in skill and "cell_id" in skill
          and "max_poll_interval_seconds" in skill
          and "Never choose the next poll" in skill,
          "missing bounded parent wait contract")
    check("pick uses the same immediate foreground wait contract",
          "This pick is also a foreground command" in skill
          and "do not start a second pick" in skill,
          "routing query can still be estimated or duplicated")
    check("handoff skill cannot synthesize a duplicate job",
          "Do not synthesize a command" in skill
          and "after a handoff handle has already been returned" in skill
          and "If `run` is missing, the equivalent is" not in skill,
          "legacy command reconstruction is still allowed")
    desc = next(
        line for line in skill.splitlines() if line.startswith("description:"))
    check("skill does not hijack every turn in the leftover repo",
          "Use when starting work" not in skill
          and "leftover scope is on" not in desc
          and "Do not use when this prompt already says you are a leftover subagent"
          in skill)
    check("skill never passes a blank --agent",
          "Never leave `--agent` empty" in skill
          and "--agent grok" in skill
          and "--agent \"$LEFTOVER_SELF\"" not in skill)
    agent_match_at = skill.find("- `agent` matches you")
    run_null_at = skill.find("- `run` is null:")
    check("skill does the work itself before it stops on a null run",
          agent_match_at != -1 and run_null_at != -1
          and agent_match_at < run_null_at,
          f"agent_match={agent_match_at}, run_null={run_null_at}")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "SKILL.md"
        dest = Path(tmp) / "claude" / "skills" / "leftover" / "SKILL.md"
        src.write_text("v1")
        dest.parent.mkdir(parents=True)
        dest.write_text("stale copy")
        link_skill(src, dest)
        check("dest is a symlink", dest.is_symlink(), str(dest))
        check("dest reads the source", dest.read_text() == "v1")
        src.write_text("v2")
        check("source edit shows up without reinstall", dest.read_text() == "v2")


def test_skill_scope_toggles_vendor_cli_influence() -> None:
    print("\n[macbot.8b] leftover scope adds or removes influence per CLI")
    from leftover.scope import (
        Cursor, apply, apply_key, dispatch, format_table, payload, resolve,
        skill_homes, snapshot)
    from leftover import macbot as macbot_mod

    check("codex alias is gpt",
          resolve("codex") == "gpt" and resolve("@agy") == "antigravity")
    check("five vendor skill homes",
          [item.key for item in skill_homes()]
          == ["claude", "gpt", "grok", "cursor", "antigravity"])
    ns = macbot_mod._parse_argv(["scope", "off", "claude", "--json"])
    check("scope off claude --json is a subcommand",
          ns.command == "scope" and ns.prompt == ["off", "claude"] and ns.json)
    ns = macbot_mod._parse_argv(["scope", "--help"])
    check("scope --help is kept", ns.command == "scope" and ns.show_help)

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        src = Path(tmp) / "SKILL.md"
        src.write_text("skill-v1")
        other = home / ".grok" / "skills" / "other" / "SKILL.md"
        other.parent.mkdir(parents=True)
        other.write_text("keep")
        grok = home / ".grok" / "skills" / "leftover" / "SKILL.md"
        claude = home / ".claude" / "skills" / "leftover" / "SKILL.md"

        apply(True, ["grok", "claude"], home=home, src=src)
        check("on links the leftover skill",
              grok.is_symlink() and grok.read_text() == "skill-v1"
              and claude.is_symlink())
        table = dispatch(["off", "grok"], home=home, src=src, interactive=False)
        check("off grok unlinks only grok",
              not grok.exists() and claude.is_symlink()
              and other.read_text() == "keep"
              and "off" in table and "Grok" in table, table)
        leftover_dir = grok.parent
        check("empty leftover dir is removed", not leftover_dir.exists())
        check("neighbor skills stay", other.read_text() == "keep")

        blob = payload(home=home)
        grok_row = next(row for row in blob["homes"] if row["key"] == "grok")
        claude_row = next(row for row in blob["homes"] if row["key"] == "claude")
        check("json payload tracks on/off",
              grok_row["on"] is False and claude_row["on"] is True)

        dispatch(["on", "@codex"], home=home, src=src, interactive=False)
        codex = home / ".codex" / "skills" / "leftover" / "SKILL.md"
        check("@codex turns Codex on", codex.is_symlink())

        try:
            dispatch(["on", "mystery"], home=home, src=src, interactive=False)
            unknown = False
        except SystemExit as exc:
            unknown = "mystery" in str(exc)
        check("unknown name is a usage error", unknown)

        try:
            dispatch(["toggle"], home=home, src=src, interactive=False)
            bad_verb = False
        except SystemExit as exc:
            bad_verb = "on|off" in str(exc)
        check("bare name is not a toggle", bad_verb)

        apply(False, ["claude", "gpt"], home=home, src=src)
        cursor = Cursor(index=0)
        apply_key(" ", cursor, home=home, src=src)
        rows = snapshot(home=home)
        check("space toggles the focused CLI",
              rows[0].key == "claude" and rows[0].on)
        apply_key("n", cursor, home=home, src=src)
        check("n turns every home off",
              not any(row.on for row in snapshot(home=home)))
        apply_key("a", cursor, home=home, src=src)
        check("a turns every home on",
              all(row.on for row in snapshot(home=home)))
        apply_key("j", cursor, home=home, src=src)
        check("j moves the cursor", cursor.index == 1)
        apply_key("2", cursor, home=home, src=src)
        rows = snapshot(home=home)
        check("digit toggles that row",
              cursor.index == 1 and rows[1].key == "gpt" and not rows[1].on)
        check("q leaves the panel",
              apply_key("q", cursor, home=home, src=src) is False)

        listed = format_table(home=home)
        check("table names leftover skill scope",
              listed.startswith("leftover skill scope") and "Codex" in listed,
              listed)
        json_text = dispatch([], home=home, as_json=True, interactive=False)
        check("scope --json is the homes list",
              json.loads(json_text)["homes"][0]["key"] == "claude")


def test_skill_scope_migrates_owned_legacy_paths() -> None:
    print("\n[macbot.8c] scope removes legacy macbot discovery paths")
    from leftover.scope import (
        apply, dispatch, install_all, payload, skill_homes, skill_source,
        status)

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        src = skill_source()
        claude = home / ".claude" / "skills" / "leftover" / "SKILL.md"
        legacy_claude = home / ".claude" / "skills" / "macbot" / "SKILL.md"
        legacy_claude.parent.mkdir(parents=True)
        legacy_claude.symlink_to(src)

        row = status("claude", home=home, src=src)
        blob = payload(home=home, src=src)
        claude_blob = next(
            item for item in blob["homes"] if item["key"] == "claude")
        check("legacy-only install does not turn the canonical switch on",
              row is not None and not row.on and row.path == claude
              and row.legacy_paths == (legacy_claude,)
              and claude_blob["on"] is False
              and claude_blob["legacy_paths"] == [str(legacy_claude)],
              repr(claude_blob))

        apply(False, ["claude"], home=home, src=src)
        check("off removes an owned legacy symlink",
              not legacy_claude.is_symlink() and not claude.exists())

        legacy_claude.parent.mkdir(parents=True, exist_ok=True)
        legacy_claude.symlink_to(src)
        apply(True, ["claude"], home=home, src=src)
        check("on migrates legacy macbot to canonical leftover",
              claude.is_symlink() and claude.resolve() == src.resolve()
              and not legacy_claude.is_symlink())

        legacy_gpt = home / ".codex" / "skills" / "macbot" / "SKILL.md"
        legacy_gpt.parent.mkdir(parents=True)
        legacy_gpt.symlink_to(src)
        install_all(home=home, src=src)
        canonical = [item.path(home) for item in skill_homes()]
        check("install-skills migrates legacy entries for every CLI",
              all(path.is_symlink() for path in canonical)
              and not legacy_gpt.is_symlink())

        unmanaged = home / ".cursor" / "skills" / "macbot" / "SKILL.md"
        unmanaged.parent.mkdir(parents=True, exist_ok=True)
        unmanaged.write_text("---\nname: somebody-else\n---\nkeep me\n")
        apply(False, ["cursor"], home=home, src=src)
        check("scope never deletes an unmanaged macbot skill",
              unmanaged.read_text().endswith("keep me\n"))

        legacy_grok = home / ".grok" / "skills" / "macbot" / "SKILL.md"
        old_checkout = (home / "old-checkout" / "leftover" / "skills"
                        / "leftover" / "SKILL.md")
        legacy_grok.parent.mkdir(parents=True, exist_ok=True)
        legacy_grok.symlink_to(old_checkout)
        check("legacy fixture is a broken owned symlink",
              legacy_grok.is_symlink() and not legacy_grok.exists())
        apply(False, ["grok"], home=home, src=src)
        check("off also removes a broken owned legacy symlink",
              not legacy_grok.is_symlink())

        leftover_only = home / ".grok" / "skills" / "macbot" / "SKILL.md"
        leftover_only.parent.mkdir(parents=True, exist_ok=True)
        leftover_only.symlink_to(src)
        dispatch([], home=home, src=src, interactive=False)
        check("leftover scope listing drops leftover-owned macbot when leftover is off",
              not leftover_only.is_symlink() and not leftover_only.exists())


def test_skill_scope_respects_cli_config_roots() -> None:
    print("\n[macbot.8d] scope honors vendor config-home overrides")
    from leftover.scope import skill_homes

    env_names = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GROK_HOME")
    original = {name: os.environ.get(name) for name in env_names}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = {
                "claude": root / "claude-config",
                "gpt": root / "codex-config",
                "grok": root / "grok-config",
            }
            os.environ["CLAUDE_CONFIG_DIR"] = str(configured["claude"])
            os.environ["CODEX_HOME"] = str(configured["gpt"])
            os.environ["GROK_HOME"] = str(configured["grok"])
            homes = {item.key: item for item in skill_homes()}
            check("config env values are complete roots before skills/leftover",
                  all(homes[key].path() == value / "skills" / "leftover"
                      / "SKILL.md"
                      for key, value in configured.items()))

            explicit = root / "explicit-home"
            check("an explicit home ignores process config overrides",
                  homes["claude"].path(explicit)
                  == explicit / ".claude" / "skills" / "leftover" / "SKILL.md"
                  and homes["gpt"].path(explicit)
                  == explicit / ".codex" / "skills" / "leftover" / "SKILL.md"
                  and homes["grok"].path(explicit)
                  == explicit / ".grok" / "skills" / "leftover" / "SKILL.md")
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_pick_rechecks_scope_before_publishing_handoff() -> None:
    print("\n[macbot.8e] cached skills obey the latest scope switch")
    from leftover import macbot as macbot_mod
    from leftover import scope as scope_mod

    spec = AgentSpec(key="gpt", label="Codex", emoji="G")
    cfg = Config(
        agents=[spec],
        routing=Routing(strategy="order", order=["gpt"],
                        coding_keys=["gpt"], plan_key="gpt"),
    )
    scope_path = Path("/tmp/codex/skills/leftover/SKILL.md")
    original_load = macbot_mod.config_mod.load
    original_decide = macbot_mod.decide
    original_status = scope_mod.status
    original_reconcile = scope_mod.reconcile
    original_snapshot = scope_mod.snapshot
    calls = {"decide": 0, "status": 0}

    async def fake_decide(cfg_arg, parsed, cwd, router=None) -> Pick:
        calls["decide"] += 1
        return Pick(spec, ["gpt"], {}, "test", parsed.kind, parsed.prompt)

    def run_pick() -> tuple[int, dict]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = macbot_mod.main([
                "--pick", "--json", "--agent", "gpt", "fix tests"])
        return code, json.loads(stdout.getvalue())

    macbot_mod.config_mod.load = lambda _path: cfg
    macbot_mod.decide = fake_decide
    scope_mod.reconcile = lambda **_kw: []
    try:
        def off_status(token: str):
            calls["status"] += 1
            return scope_mod.Row("gpt", "Codex", scope_path, False)

        scope_mod.status = off_status
        code, blob = run_pick()
        check("off bypasses routing even when a cached skill invokes pick",
              code == 0 and calls == {"decide": 0, "status": 1}
              and blob["kind"] == "coding" and blob["agent"] == "gpt"
              and blob["run"] is None and blob["spawn"] is None
              and blob["announce"] == ""
              and blob["scope"]["active"] is False,
              repr(blob))

        calls.update(decide=0, status=0)

        def on_status(token: str):
            calls["status"] += 1
            return scope_mod.Row("gpt", "Codex", scope_path, True)

        scope_mod.status = on_status
        code, blob = run_pick()
        check("on + caller is chosen agent publishes no leftover --print",
              code == 0 and calls == {"decide": 1, "status": 2}
              and blob["agent"] == "gpt"
              and blob["reason"] == "test"
              and blob["run"] is None and blob["spawn"] is None
              and blob["announce"] == ""
              and blob["scope"]["active"] is True,
              repr(blob))

        calls.update(decide=0, status=0)
        grok_spec = AgentSpec(key="grok", label="Grok", emoji="X")

        async def fake_decide_other(cfg_arg, parsed, cwd, router=None) -> Pick:
            calls["decide"] += 1
            return Pick(grok_spec, ["grok"], {}, "test", parsed.kind, parsed.prompt)

        macbot_mod.decide = fake_decide_other
        code, blob = run_pick()
        check("on hands off to someone else with leftover --print",
              code == 0 and calls == {"decide": 1, "status": 2}
              and blob["agent"] == "grok"
              and blob["run"][:4] == ["leftover", "--print", "--use", "grok"]
              and blob["scope"]["active"] is True,
              repr(blob))
        macbot_mod.decide = fake_decide

        calls.update(decide=0, status=0)
        states = iter((True, False))

        def changing_status(token: str):
            calls["status"] += 1
            return scope_mod.Row(
                "gpt", "Codex", scope_path, next(states))

        scope_mod.status = changing_status
        code, blob = run_pick()
        check("an off toggle during decide wins before handoff is returned",
              code == 0 and calls == {"decide": 1, "status": 2}
              and blob["agent"] == "gpt" and blob["run"] is None
              and blob["scope"]["active"] is False
              and "work directly" in blob["reason"],
              repr(blob))

        calls.update(decide=0, status=0)
        scope_mod.snapshot = lambda **_kw: [
            scope_mod.Row("gpt", "Codex", scope_path, False),
            scope_mod.Row("grok", "Grok", Path("/tmp/grok/skills/leftover/SKILL.md"),
                          False),
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = macbot_mod.main(["--pick", "--json", "fix tests"])
        blob = json.loads(stdout.getvalue())
        check("pick without --agent still bypasses when every CLI is off",
              code == 0 and calls["decide"] == 0
              and blob["run"] is None
              and blob["scope"]["active"] is False,
              repr(blob))

        calls.update(decide=0, status=0)
        scope_mod.snapshot = lambda **_kw: [
            scope_mod.Row("gpt", "Codex", scope_path, False),
            scope_mod.Row("grok", "Grok", Path("/tmp/grok/skills/leftover/SKILL.md"),
                          True),
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = macbot_mod.main(
                ["--pick", "--json", "--agent", "", "fix tests"])
        blob = json.loads(stdout.getvalue())
        check("explicit empty --agent bypasses even when another CLI is on",
              code == 0 and calls["decide"] == 0
              and blob["run"] is None and blob["spawn"] is None
              and blob["announce"] == ""
              and blob["scope"]["active"] is False,
              repr((calls, blob)))

        calls.update(decide=0, status=0)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = macbot_mod.main(["--pick", "--json", "fix tests"])
        blob = json.loads(stdout.getvalue())
        check("omitted --agent still routes when some CLI is on",
              code == 0 and calls["decide"] == 1
              and blob["agent"] == "gpt"
              and blob["run"][:4] == ["leftover", "--print", "--use", "gpt"],
              repr((calls, blob)))

        calls.update(decide=0, status=0)
        scope_mod.status = off_status
        os.environ["LEFTOVER_SELF"] = "gpt"
        try:
            code, blob = run_pick_without_agent()
            check("LEFTOVER_SELF is caller identity when --agent is omitted",
                  code == 0 and calls == {"decide": 0, "status": 1}
                  and blob.get("self") == "gpt"
                  and blob["run"] is None
                  and blob["spawn"] is None
                  and blob["announce"] == ""
                  and blob["scope"]["active"] is False,
                  repr((calls, blob)))
        finally:
            os.environ.pop("LEFTOVER_SELF", None)
    finally:
        scope_mod.status = original_status
        scope_mod.reconcile = original_reconcile
        scope_mod.snapshot = original_snapshot
        macbot_mod.decide = original_decide
        macbot_mod.config_mod.load = original_load


def run_pick_without_agent() -> tuple[int, dict]:
    from leftover import macbot as macbot_mod
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = macbot_mod.main(["--pick", "--json", "fix tests"])
    return code, json.loads(stdout.getvalue())


def test_spawned_cli_gets_leftover_self() -> None:
    print("\n[macbot.8f] leftover-spawned CLIs know who they already are")
    from leftover.agents.base import child_env

    spec = AgentSpec(key="grok", label="Grok", emoji="X", env={"FOO": "1"})
    env = child_env(spec)
    check("worker env names who leftover spawned",
          env["LEFTOVER_SELF"] == "grok" and env["FOO"] == "1")


def test_quota_serde() -> None:
    print("\n[macbot.6] quota snapshot round-trip")
    now = 1_000_000.0
    original = Quota(agent="grok", note="sg", checked_at=now, windows=[
        Window("weekly", 59.0, now + 3600, REPORTED, "billing")])
    back = Quota.from_dict(original.to_dict())
    check("from_dict rejects junk", Quota.from_dict("nope") is None)
    check("round-trip agent", back is not None and back.agent == "grok")
    check("round-trip window",
          back is not None and back.windows[0].used_percent == 59.0
          and back.windows[0].source == REPORTED)

    from leftover.rhythm import payload
    spec = AgentSpec(key="grok", label="Grok", emoji="X")
    empty = Quota(agent="antigravity", note="no vendor number", windows=[])
    blob = payload(
        [(spec, original, None),
         (AgentSpec(key="antigravity", label="Antigravity", emoji="A"),
          empty, None)],
        now=now, strategy="lag_waste", order=["gpt", "grok"],
        tz_name="UTC")
    grok = blob["agents"][0]
    agy = blob["agents"][1]
    check("quota JSON is the same windows /quota already shows",
          blob["strategy"] == "lag_waste" and grok["source"] == REPORTED
          and grok["windows"][0]["used_percent"] == 59.0
          and grok["windows"][0]["name"] == "weekly"
          and agy["note"] == "no vendor number"
          and agy["windows"] == [], repr(blob))
    check("quota JSON does not grow a strength or token field",
          "strength" not in blob and "token" not in grok
          and "accessToken" not in json.dumps(blob))


async def test_quota_disk_cache() -> None:
    print("\n[macbot.7] quota cache survives a new process")
    with tempfile.TemporaryDirectory() as tmp:
        agents = [
            AgentSpec(key="claude", label="Claude", emoji="C",
                      interactive_command=["true"], exec_command=["true"]),
            AgentSpec(key="gpt", label="Codex", emoji="G",
                      interactive_command=["true"], exec_command=["true"],
                      budget_5h_turns=10, budget_week_turns=40),
            AgentSpec(key="grok", label="Grok", emoji="X",
                      interactive_command=["true"], exec_command=["true"]),
            AgentSpec(key="cursor", label="Cursor", emoji="K",
                      interactive_command=["true"], exec_command=["true"]),
        ]
        cfg = Config(agents=agents, data_dir=tmp,
                     routing=Routing(strategy="lag_waste",
                                     coding_keys=["gpt", "grok", "cursor"],
                                     plan_key="claude",
                                     quota_ttl=120))
        await decide(cfg, intent_mod.parse("fix tests"), tmp)
        state = load_state(cfg)
        cached = (state.get("quota") or {}).get("gpt") or {}
        check("decide writes quota into macbot-state.json",
              bool(cached.get("windows")), str(cached))
        checked = float(cached.get("checked_at") or 0)
        await decide(cfg, intent_mod.parse("fix tests again"), tmp)
        again = (load_state(cfg).get("quota") or {}).get("gpt") or {}
        check("fresh cache is reused, not re-probed",
              abs(float(again.get("checked_at") or 0) - checked) < 1e-6,
              f"{again.get('checked_at')} vs {checked}")
        state["quota"]["gpt"]["checked_at"] = time.time() - 1000
        save_state(cfg, state)
        await decide(cfg, intent_mod.parse("fix after stale"), tmp)
        refreshed = (load_state(cfg).get("quota") or {}).get("gpt") or {}
        check("stale cache is refreshed",
              float(refreshed.get("checked_at") or 0) > time.time() - 5,
              str(refreshed.get("checked_at")))


async def test_run_print_uses_current_workdir() -> None:
    print("\n[macbot.9] --print inherits the caller workdir")
    from leftover import agents as agents_mod

    class RecordingPool:
        seen_workdir = ""

        def __init__(self, config: Config) -> None:
            self.config = config

        async def set_workdir(self, path: str) -> None:
            type(self).seen_workdir = path

        def peek(self, spec: AgentSpec):
            return None

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            return Turn(agent=spec, text="ok")

        async def shutdown(self) -> None:
            return None

    with tempfile.TemporaryDirectory() as tmp:
        spec = AgentSpec(key="gpt", label="Codex", emoji="G")
        cfg = Config(
            agents=[spec],
            default_workdir=str(Path(tmp) / "wrong-default"),
            data_dir=tmp,
            routing=Routing(strategy="order", order=["gpt"],
                            coding_keys=["gpt"], plan_key="gpt"),
        )
        pick = Pick(spec, ["gpt"], {}, "test", "coding", "check cwd")
        original = agents_mod.AgentPool
        agents_mod.AgentPool = RecordingPool
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = await run_print(cfg, pick)
        finally:
            agents_mod.AgentPool = original
        check("headless worker starts in os.getcwd()",
              code == 0 and RecordingPool.seen_workdir == os.getcwd(),
              RecordingPool.seen_workdir)


async def test_run_print_respects_pick_chain() -> None:
    print("\n[macbot.10] --print executes the decided chain exactly")
    from leftover import agents as agents_mod

    class ChainPool:
        attempts: list[str] = []
        mode = "ordered"

        def __init__(self, config: Config) -> None:
            self.config = config

        async def set_workdir(self, path: str) -> None:
            return None

        def peek(self, spec: AgentSpec):
            return None

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            type(self).attempts.append(spec.key)
            if spec.key == "gpt":
                if type(self).mode == "cu":
                    return Turn(agent=spec,
                                text="You've hit your weekly limit")
                return Turn(agent=spec, error="connection reset by peer")
            return Turn(agent=spec, text=f"{spec.key} handled it")

        async def shutdown(self) -> None:
            return None

    agents = [
        AgentSpec(key="gpt", label="Codex", fallback=["claude"]),
        AgentSpec(key="grok", label="Grok"),
        AgentSpec(key="cursor", label="Cursor"),
        AgentSpec(key="claude", label="Claude"),
    ]
    original = agents_mod.AgentPool
    agents_mod.AgentPool = ChainPool
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                agents=agents,
                data_dir=tmp,
                routing=Routing(strategy="order",
                                order=["gpt", "grok", "cursor", "claude"]),
            )
            ChainPool.mode = "ordered"
            ChainPool.attempts = []
            pick = Pick(agents[0], ["gpt", "grok", "cursor", "claude"],
                        {}, "test", "coding", "fix tests")
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                code = await run_print(cfg, pick)
            chatter = stderr.getvalue()
            check("agent fallbacks cannot reorder pick.chain",
                  code == 0 and ChainPool.attempts == ["gpt", "grok"],
                  str(ChainPool.attempts))
            check("failover chatter is usher-shaped",
                  "→ Codex  (coding · headless)" in chatter
                  and "failing over to Grok" in chatter
                  and "with continuation notice" in chatter,
                  chatter)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                agents=agents,
                data_dir=tmp,
                routing=Routing(strategy="order",
                                order=["gpt", "grok", "cursor", "claude"]),
            )
            ChainPool.mode = "cu"
            ChainPool.attempts = []
            pick = Pick(agents[0], ["gpt"], {}, "test", "computer_use",
                        "click settings")
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = await run_print(cfg, pick)
            check("computer use never falls back beyond Codex",
                  code == 1 and ChainPool.attempts == ["gpt"],
                  str(ChainPool.attempts))

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                agents=agents,
                data_dir=tmp,
                routing=Routing(
                    strategy="order",
                    order=["gpt", "grok", "cursor", "claude"],
                    max_attempts=1),
            )
            ChainPool.mode = "ordered"
            ChainPool.attempts = []
            pick = Pick(agents[0], ["gpt", "grok", "cursor", "claude"],
                        {}, "test", "coding", "fix tests")
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = await run_print(cfg, pick)
            check("ordinary --print respects configured max_attempts",
                  code == 1 and ChainPool.attempts == ["gpt"],
                  str(ChainPool.attempts))
    finally:
        agents_mod.AgentPool = original


async def test_progress_is_visible_and_output_stays_clean() -> None:
    print("\n[macbot.10b] long turns report progress on stderr")
    from leftover import agents as agents_mod
    from leftover.agents.base import Event
    from leftover.macbot import _discuss, _speak
    from leftover.router import Router
    from leftover.transcript import Transcript

    class ProgressPool:
        shutdowns = 0
        raise_in_run = False

        def __init__(self, config: Config | None = None) -> None:
            self.config = config

        async def set_workdir(self, path: str) -> None:
            return None

        def peek(self, spec: AgentSpec):
            return None

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            if type(self).raise_in_run:
                raise RuntimeError("runner exploded")
            if on_event is not None:
                await on_event(Event(
                    "thought", "looking at leftover progress rendering now"))
                await on_event(Event(
                    "status", "surface the in-progress plan step"))
                await on_event(Event("tool", "inspect repository leftover/macbot.py"))
            await asyncio.sleep(0.035)
            return Turn(agent=spec, text=f"FINAL {spec.key}")

        async def shutdown(self) -> None:
            type(self).shutdowns += 1

    agents = [
        AgentSpec(key="gpt", label="Codex", interactive_command=["true"]),
        AgentSpec(key="grok", label="Grok", interactive_command=["true"]),
        AgentSpec(key="cursor", label="Cursor", interactive_command=["true"]),
    ]
    original = agents_mod.AgentPool
    agents_mod.AgentPool = ProgressPool
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                agents=agents, data_dir=tmp,
                routing=Routing(strategy="order", order=[a.key for a in agents],
                                coding_keys=[a.key for a in agents],
                                max_attempts=1),
            )
            pick = Pick(agents[0], [a.key for a in agents], {}, "test",
                        "coding", "inspect")
            stdout, stderr = io.StringIO(), io.StringIO()
            ProgressPool.shutdowns = 0
            ProgressPool.raise_in_run = False
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                code = await run_print(cfg, pick, heartbeat_seconds=0.01)
            progress = stderr.getvalue()
            check("--print stdout contains only the final answer",
                  code == 0 and stdout.getvalue() == "FINAL gpt\n",
                  repr(stdout.getvalue()))
            check("--print reports attempts and tools on stderr",
                  "→ Codex  (coding · headless)" in progress
                  and "leftover: Codex tool: inspect repository leftover/macbot.py"
                  in progress,
                  repr(progress))
            check("--print reports thought and plan status on stderr",
                  "leftover: Codex: looking at leftover progress rendering now"
                  in progress
                  and "leftover: Codex: surface the in-progress plan step"
                  in progress,
                  repr(progress))
            check("--print stdout stays the final answer",
                  "looking at leftover" not in stdout.getvalue()
                  and "surface the in-progress" not in stdout.getvalue(),
                  repr(stdout.getvalue()))
            check("--print emits a heartbeat during a quiet interval",
                  "leftover: still working (Codex · inspect repository leftover/macbot.py)"
                  in progress,
                  repr(progress))
            check("--print shuts its pool down after success",
                  ProgressPool.shutdowns == 1,
                  str(ProgressPool.shutdowns))

            ProgressPool.raise_in_run = True
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                try:
                    await run_print(cfg, pick, heartbeat_seconds=0.01)
                except RuntimeError as exc:
                    raised = str(exc)
                else:
                    raised = ""
            check("--print shuts its pool down after an exception",
                  raised == "runner exploded" and ProgressPool.shutdowns == 2,
                  f"raised={raised!r}, shutdowns={ProgressPool.shutdowns}")

        with tempfile.TemporaryDirectory() as tmp:
            ProgressPool.raise_in_run = False
            cfg = Config(
                agents=agents, data_dir=tmp,
                routing=Routing(strategy="order", order=[a.key for a in agents],
                                coding_keys=[a.key for a in agents],
                                max_attempts=1),
            )
            pool = ProgressPool(cfg)
            router = Router(cfg, pool)
            pick = Pick(agents[0], ["gpt"], {}, "test", "coding", "inspect")
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(stderr):
                await _speak(
                    cfg, router, Transcript(), pick, "inspect",
                    heartbeat_seconds=0.01)
            check("interactive turns heartbeat without corrupting streamed text",
                  "leftover: still working (Codex · inspect repository leftover/macbot.py)"
                  in stderr.getvalue(),
                  repr(stderr.getvalue()))

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                ok = await _discuss(
                    cfg, router, Transcript(),
                    intent_mod.parse("/rt @gpt @grok compare"),
                    heartbeat_seconds=0.01)
            check("group discussions heartbeat during quiet agents",
                  ok and "leftover: still working" in stderr.getvalue(),
                  repr(stderr.getvalue()))
            check("group answer blocks include role and round metadata",
                  "speaker 1 · round 1" in stdout.getvalue()
                  and "speaker 2 · round 1" in stdout.getvalue(),
                  repr(stdout.getvalue()))
    finally:
        agents_mod.AgentPool = original


async def test_print_long_running_tool_does_not_exit_124() -> None:
    print("\n[macbot.10c] --print keeps a quiet in-flight tool alive")
    mock = str(ROOT / "tests" / "mock_acp_agent.py")
    idle = 0.05
    tool_seconds = 0.2
    with tempfile.TemporaryDirectory() as tmp:
        spec = AgentSpec(
            key="cursor", label="Cursor", transport="acp",
            acp_command=[sys.executable, mock],
            env={"MOCK_BEHAVIOR": "long_tool",
                 "MOCK_TOOL_SECONDS": str(tool_seconds)},
            timeout=2, acp_idle_timeout=idle)
        cfg = Config(
            agents=[spec], data_dir=tmp,
            routing=Routing(strategy="order", order=["cursor"],
                            coding_keys=["cursor"], max_attempts=1))
        pick = Pick(spec, ["cursor"], {}, "test", "coding", "run pytest")
        stdout, stderr = io.StringIO(), io.StringIO()
        loop = asyncio.get_running_loop()
        started = loop.time()
        polls = 0
        alive_past_idle = False
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            task = asyncio.create_task(
                run_print(cfg, pick, heartbeat_seconds=0.05))
            # Skill contract: wait on process exit, poll at a compressed
            # analog of completion.max_poll_interval_seconds.
            while not task.done():
                await asyncio.sleep(0.02)
                polls += 1
                if not task.done() and loop.time() - started > idle:
                    alive_past_idle = True
            code = await task
        elapsed = loop.time() - started
        chatter = stderr.getvalue()
        check("a quiet pytest-like tool does not exit 124",
              code == 0 and stdout.getvalue() == "tests passed\n",
              f"code={code} stdout={stdout.getvalue()!r} elapsed={elapsed:.3f}s "
              f"stderr={chatter!r}")
        check("the --print process stays alive past the idle boundary",
              alive_past_idle and elapsed > idle and polls >= 2,
              f"alive={alive_past_idle} elapsed={elapsed:.3f}s polls={polls}")
        check("--print heartbeats during the quiet in-flight tool",
              "leftover: still working" in chatter
              and "pytest" in chatter,
              chatter)


async def test_group_routes_survive_cli_handoff() -> None:
    print("\n[macbot.11] group routes survive --pick and --print")
    from leftover import agents as agents_mod

    class GroupPool:
        attempts: list[str] = []

        def __init__(self, config: Config) -> None:
            self.config = config

        async def set_workdir(self, path: str) -> None:
            return None

        def peek(self, spec: AgentSpec):
            return None

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            type(self).attempts.append(spec.key)
            return Turn(agent=spec, text=f"{spec.key} answered")

        async def shutdown(self) -> None:
            return None

    agents = [
        AgentSpec(key="claude", label="Claude", interactive_command=["true"]),
        AgentSpec(key="gpt", label="Codex", interactive_command=["true"]),
        AgentSpec(key="grok", label="Grok", interactive_command=["true"]),
        AgentSpec(key="cursor", label="Cursor", interactive_command=["true"]),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(agents=agents, data_dir=tmp)
        group = await decide(cfg, intent_mod.parse("/rt compare routes"), tmp)
        blob = group.as_dict()
        check("group pick is a panel, not one agent",
              group.available and group.spec is None
              and blob["agents"] == ["claude", "gpt", "grok", "cursor"],
              str(blob.get("agents")))
        check("group run preserves mode and exact panel",
              blob["run"] == [
                  "leftover", "--print",
                  "/rt @claude @gpt @grok @cursor compare routes"],
              str(blob["run"]))
        unknown = await decide(
            cfg, intent_mod.parse("@gpt @missing compare routes"), tmp)
        check("secondary unknown mentions are rejected",
              not unknown.available and "@missing" in unknown.reason,
              unknown.reason)

        original = agents_mod.AgentPool
        agents_mod.AgentPool = GroupPool
        GroupPool.attempts = []
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = await run_discuss(
                    cfg, intent_mod.parse("/rt @claude @gpt compare routes"))
            first_attempts = list(GroupPool.attempts)
            GroupPool.attempts = []

            class BrokenStreamSink:
                def __init__(self, *_args, **_kwargs):
                    pass

                async def __call__(self, _event):
                    raise RuntimeError("output closed")

            original_sink = ui_mod.StreamSink
            ui_mod.StreamSink = BrokenStreamSink
            failed_stdout = io.StringIO()
            try:
                with contextlib.redirect_stdout(failed_stdout), \
                        contextlib.redirect_stderr(io.StringIO()):
                    failed_code = await run_discuss(
                        cfg,
                        intent_mod.parse(
                            "/all @claude @gpt compare delivery"),
                    )
            finally:
                ui_mod.StreamSink = original_sink
        finally:
            agents_mod.AgentPool = original
        check("headless roundtable runs every selected panel member",
              code == 0 and first_attempts == ["claude", "gpt"],
              str(first_attempts))
        check("headless group delivery failure returns nonzero",
              failed_code == 1
              and "delivery failed: RuntimeError: output closed"
              in failed_stdout.getvalue(),
              repr(failed_stdout.getvalue()))

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(agents=agents[:2], data_dir=tmp)
        debate = await decide(cfg, intent_mod.parse("/debate ship it"), tmp)
        check("debate requires two sides and a judge",
              not debate.available and debate.as_dict()["run"] is None
              and "at least 3" in debate.reason,
              debate.reason)


def test_builtin_acp_commands() -> None:
    print("\n[macbot.12] built-in ACP commands match current CLIs")
    commands = {agent["key"]: agent.get("acp_command", [])
                for agent in BUILTIN_AGENTS}
    check("Codex uses the maintained ACP adapter",
          commands["gpt"] == [
              "npx", "-y", "@agentclientprotocol/codex-acp@1.6.2"],
          str(commands["gpt"]))
    check("Cursor uses the ACP subcommand and pins first-party Grok",
          commands["cursor"] == [
              "cursor-agent", "--model", "grok-4.6", "acp"],
          str(commands["cursor"]))
    grok = next(agent for agent in BUILTIN_AGENTS if agent["key"] == "grok")
    check("Grok leaves -p adjacent to the appended fallback prompt",
          grok["exec_command"][-1] == "-p", str(grok["exec_command"]))


def test_antigravity_is_exec_only_and_stays_first_party() -> None:
    print("\n[macbot.12c] Antigravity spec matches the real `agy` CLI")
    from leftover.agents.exec_runner import _error_from_json, _text_from_json
    from leftover.config import Routing, _agent_from_dict

    spec_dict = next(agent for agent in BUILTIN_AGENTS
                     if agent["key"] == "antigravity")
    argv = spec_dict["exec_command"]
    check("agy 1.1.19 has no ACP mode, so this one is exec-only",
          spec_dict["transport"] == "exec" and not spec_dict.get("acp_command"),
          str(spec_dict.get("acp_command")))
    check("-p stays adjacent to the appended prompt",
          argv[-1] == "-p", str(argv))
    check("tool permissions are auto-approved like every other backend",
          "--dangerously-skip-permissions" in argv, str(argv))
    check("answers are read out of the JSON `response` field",
          spec_dict["exec_output"] == "json"
          and spec_dict["exec_json_path"] == "response")

    model = argv[argv.index("--model") + 1]
    check("pinned to first-party Gemini, never Claude or GPT",
          model.startswith("gemini-")
          and not any(token in model for token in ("claude", "gpt")), model)

    # agy defaults to a 5m print timeout and would abandon the turn while
    # leftover was still waiting on its own deadline.
    print_timeout = argv[argv.index("--print-timeout") + 1]
    check("agy's own print deadline covers the leftover turn timeout",
          print_timeout == "15m" and spec_dict["timeout"] <= 15 * 60,
          f"{print_timeout} vs {spec_dict['timeout']}s")

    routing = Routing()
    check("in the coding pool, but last",
          routing.coding_keys[-1] == "antigravity"
          and routing.order[-1] == "antigravity",
          f"{routing.coding_keys} / {routing.order}")
    check("not the planner and not the computer-use backend",
          routing.plan_key == "claude" and routing.cu_key == "gpt")

    # Envelopes captured from agy 1.1.19. It answers 0 even when it fails, so
    # the JSON body is the only failure signal.
    spec = _agent_from_dict(spec_dict)
    ok_envelope = {"conversation_id": "abc", "status": "SUCCESS",
                   "response": "PINNED\n", "duration_seconds": 7.28,
                   "num_turns": 1, "usage": {"total_tokens": 14154}}
    err_envelope = {"conversation_id": "", "status": "ERROR", "response": "",
                    "error": "invalid model selection (--model \"nope\")",
                    "duration_seconds": 0, "num_turns": 0}
    check("a successful envelope yields the answer",
          _text_from_json(ok_envelope, spec) == "PINNED\n"
          and _error_from_json(ok_envelope) == "")
    check("a failed envelope yields the error even though agy exited 0",
          _error_from_json(err_envelope).startswith("invalid model selection"),
          _error_from_json(err_envelope))


async def test_acp_start_failure_closes_transport() -> None:
    print("\n[macbot.13] failed ACP handshakes close deterministically")
    from leftover.agents import acp_runner as acp_mod

    state = {"entered": 0, "exited": 0}

    class ClosedConnection:
        async def initialize(self, **kwargs):
            raise ConnectionError("Connection closed")

        async def close(self) -> None:
            return None

    @contextlib.asynccontextmanager
    async def fake_spawn(*args, **kwargs):
        state["entered"] += 1
        try:
            yield ClosedConnection(), object()
        finally:
            state["exited"] += 1

    original = acp_mod.spawn_agent_process
    acp_mod.spawn_agent_process = fake_spawn
    runner = acp_mod.AcpRunner(AgentSpec(
        key="broken", label="Broken", acp_command=["broken-acp"]))
    failed = False
    try:
        try:
            await runner.start(os.getcwd())
        except ConnectionError:
            failed = True
        check("failed initialize exits its process context immediately",
              failed and state == {"entered": 1, "exited": 1}, str(state))
        check("failed initialize publishes no live session",
              not runner.live_session() and runner._stack is None)
    finally:
        acp_mod.spawn_agent_process = original
        await runner.close()


async def test_acp_concurrent_start_is_singleton() -> None:
    print("\n[macbot.14] concurrent ACP starts share one process and session")
    from leftover.agents import acp_runner as acp_mod

    state = {"entered": 0, "exited": 0, "initialized": 0, "sessions": 0}

    class Session:
        session_id = "shared-session"

    class Connection:
        async def initialize(self, **kwargs):
            state["initialized"] += 1
            await asyncio.sleep(0.02)

        async def new_session(self, **kwargs):
            state["sessions"] += 1
            return Session()

    @contextlib.asynccontextmanager
    async def fake_spawn(*args, **kwargs):
        state["entered"] += 1
        try:
            yield Connection(), object()
        finally:
            state["exited"] += 1

    original = acp_mod.spawn_agent_process
    acp_mod.spawn_agent_process = fake_spawn
    runner = acp_mod.AcpRunner(AgentSpec(
        key="shared", label="Shared", acp_command=["shared-acp"]))
    try:
        await asyncio.gather(runner.start(os.getcwd()), runner.start(os.getcwd()))
        check("only one ACP process and session are created",
              state == {
                  "entered": 1, "exited": 0,
                  "initialized": 1, "sessions": 1,
              }, str(state))
        check("the shared session is live",
              runner.live_session() and runner.session_id == "shared-session")
    finally:
        await runner.close()
        acp_mod.spawn_agent_process = original
    check("the shared process closes exactly once", state["exited"] == 1,
          str(state))


async def test_acp_cancel_then_prompt_ignores_stale_done() -> None:
    print("\n[macbot.15] ACP cancellation leaves the next prompt clean")
    from leftover.agents import acp_runner as acp_mod

    first_started = asyncio.Event()
    first_release = asyncio.Event()
    second_started = asyncio.Event()
    second_release = asyncio.Event()
    state = {"cancel_calls": 0, "first_prompt_finished": False, "starts": 0}

    class Result:
        stop_reason = "end_turn"

    class Connection:
        async def prompt(self, session_id, prompt):
            text = prompt[0].text
            if text == "first":
                first_started.set()
                await first_release.wait()
                state["first_prompt_finished"] = True
                return Result()
            second_started.set()
            await second_release.wait()
            await runner._queue.put(acp_mod.Event("text", "SECOND"))
            return Result()

        async def cancel(self, session_id):
            state["cancel_calls"] += 1
            first_release.set()

    class RebuildingRunner(acp_mod.AcpRunner):
        async def start(self, workdir: str) -> None:
            state["starts"] += 1
            self._workdir = os.path.realpath(workdir)
            self._conn = Connection()
            self._session_id = f"session-{state['starts']}"

    runner = RebuildingRunner(AgentSpec(
        key="cancel", label="Cancel", acp_command=["unused"], timeout=2))
    runner._conn = Connection()
    runner._session_id = "session"
    runner._workdir = os.getcwd()

    async def collect(prompt: str):
        return [event async for event in runner.stream(prompt)]

    try:
        first = asyncio.create_task(collect("first"))
        await first_started.wait()
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first

        second = asyncio.create_task(collect("second"))
        await second_started.wait()
        await runner._queue.put((acp_mod._DONE, object()))
        await asyncio.sleep(0)
        check("a stale completion token cannot finish the new prompt",
              not second.done())
        second_release.set()
        events = await second
        check("cancel retires the old session before rebuilding",
              state == {
                  "cancel_calls": 1, "first_prompt_finished": True,
                  "starts": 1,
              }, str(state))
        check("the next prompt contains only its own output and completion",
              [(event.kind, event.text) for event in events]
              == [("text", "SECOND"), ("done", "")],
              repr([(event.kind, event.text) for event in events]))
    finally:
        first_release.set()
        second_release.set()
        await runner.close()


async def test_acp_cancel_rpc_is_bounded() -> None:
    print("\n[macbot.16] a stuck ACP cancel RPC cannot hang cleanup")
    from leftover.agents import acp_runner as acp_mod

    state = {"cancel_finished": False, "prompt_finished": False}

    class Connection:
        async def prompt(self, session_id, prompt):
            try:
                await asyncio.Event().wait()
            finally:
                state["prompt_finished"] = True

        async def cancel(self, session_id):
            try:
                await asyncio.Event().wait()
            finally:
                state["cancel_finished"] = True

    runner = acp_mod.AcpRunner(AgentSpec(
        key="stuck", label="Stuck", acp_command=["unused"], timeout=0.01))
    runner._conn = Connection()
    runner._session_id = "session"
    runner._workdir = os.getcwd()
    original_rpc = acp_mod._CANCEL_RPC_TIMEOUT
    original_grace = acp_mod._CANCEL_GRACE_TIMEOUT
    acp_mod._CANCEL_RPC_TIMEOUT = 0.02
    acp_mod._CANCEL_GRACE_TIMEOUT = 0.02
    started = asyncio.get_running_loop().time()
    try:
        turn = await asyncio.wait_for(runner.run("timeout"), timeout=0.3)
    finally:
        acp_mod._CANCEL_RPC_TIMEOUT = original_rpc
        acp_mod._CANCEL_GRACE_TIMEOUT = original_grace
    elapsed = asyncio.get_running_loop().time() - started
    await asyncio.sleep(0)
    check("timeout cleanup remains bounded", elapsed < 0.2, f"{elapsed:.3f}s")
    check("both stuck coroutines are reaped before run returns",
          state == {"cancel_finished": True, "prompt_finished": True},
          str(state))
    check("the timed-out session is invalidated",
          turn.error == "timed out after 0.01s" and not runner.live_session(),
          str(turn.error))


async def test_acp_forced_restart_drops_late_updates() -> None:
    print("\n[macbot.17] uncertain cancellation rebuilds the ACP session")
    from leftover.agents import acp_runner as acp_mod

    state = {"spawns": 0, "closes": 0, "late_delivered": 0}
    background: set[asyncio.Task] = set()

    class Session:
        def __init__(self, index: int) -> None:
            self.session_id = f"session-{index}"

    class Result:
        stop_reason = "end_turn"

    class Connection:
        def __init__(self, index: int, closed: asyncio.Event) -> None:
            self.index = index
            self.closed = closed
            self.prompts = 0

        async def initialize(self, **kwargs):
            return None

        async def new_session(self, **kwargs):
            return Session(self.index)

        async def prompt(self, session_id, prompt):
            self.prompts += 1
            if self.index == 1 and self.prompts == 1:
                await asyncio.Event().wait()
            if self.index == 1:
                await asyncio.sleep(0.06)
            await runner._queue.put(acp_mod.Event("text", "NEW"))
            return Result()

        async def cancel(self, session_id):
            if self.index != 1:
                return

            async def late_update() -> None:
                await asyncio.sleep(0.04)
                if not self.closed.is_set():
                    state["late_delivered"] += 1
                    await runner._queue.put(acp_mod.Event("text", "OLD"))

            task = asyncio.create_task(late_update())
            background.add(task)
            task.add_done_callback(background.discard)

    @contextlib.asynccontextmanager
    async def fake_spawn(*args, **kwargs):
        state["spawns"] += 1
        closed = asyncio.Event()
        try:
            yield Connection(state["spawns"], closed), object()
        finally:
            closed.set()
            state["closes"] += 1
            tasks = list(background)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    original_spawn = acp_mod.spawn_agent_process
    original_rpc = acp_mod._CANCEL_RPC_TIMEOUT
    original_grace = acp_mod._CANCEL_GRACE_TIMEOUT
    acp_mod.spawn_agent_process = fake_spawn
    acp_mod._CANCEL_RPC_TIMEOUT = 0.02
    acp_mod._CANCEL_GRACE_TIMEOUT = 0.01
    runner = acp_mod.AcpRunner(AgentSpec(
        key="late", label="Late", acp_command=["fake"], timeout=0.01))
    try:
        first = await runner.run("first")
        second = await runner.run("second")
        check("the uncertain first turn reports its timeout",
              first.error == "timed out after 0.01s", str(first.error))
        check("the next turn uses a fresh ACP process and session",
              state["spawns"] == 2 and runner.session_id == "session-2",
              str(state))
        check("late output from the abandoned session is not delivered",
              state["late_delivered"] == 0 and second.text == "NEW",
              f"{state}, text={second.text!r}")
    finally:
        await runner.close()
        acp_mod.spawn_agent_process = original_spawn
        acp_mod._CANCEL_RPC_TIMEOUT = original_rpc
        acp_mod._CANCEL_GRACE_TIMEOUT = original_grace


async def test_acp_sink_error_closes_stream() -> None:
    print("\n[macbot.18] event sink failures close the ACP stream immediately")
    from leftover.agents import acp_runner as acp_mod

    release = asyncio.Event()
    state = {"prompt_finished": False}

    class Result:
        stop_reason = "end_turn"

    class Connection:
        async def prompt(self, session_id, prompt):
            await runner._queue.put(acp_mod.Event("text", "chunk"))
            await release.wait()
            state["prompt_finished"] = True
            return Result()

        async def cancel(self, session_id):
            release.set()

    async def broken_sink(event):
        raise RuntimeError("sink failed")

    runner = acp_mod.AcpRunner(AgentSpec(
        key="sink", label="Sink", acp_command=["unused"], timeout=1))
    runner._conn = Connection()
    runner._session_id = "session"
    runner._workdir = os.getcwd()
    turn = await runner.run("prompt", broken_sink)
    check("the sink error is returned on the turn",
          turn.error == "RuntimeError: sink failed", str(turn.error))
    check("run returns only after prompt cleanup releases the lock",
          state["prompt_finished"] and not runner._lock.locked(), str(state))


async def test_acp_concurrent_workdirs_do_not_cross() -> None:
    print("\n[macbot.19] one ACP session cannot silently switch workdirs")
    from leftover.agents import acp_runner as acp_mod

    entered = asyncio.Event()
    release = asyncio.Event()
    state = {"cwd": None}

    class Session:
        session_id = "cwd-session"

    class Connection:
        async def initialize(self, **kwargs):
            entered.set()
            await release.wait()

        async def new_session(self, **kwargs):
            return Session()

    @contextlib.asynccontextmanager
    async def fake_spawn(*args, **kwargs):
        state["cwd"] = kwargs["cwd"]
        yield Connection(), object()

    original = acp_mod.spawn_agent_process
    acp_mod.spawn_agent_process = fake_spawn
    runner = acp_mod.AcpRunner(AgentSpec(
        key="cwd", label="Cwd", acp_command=["fake"]))
    try:
        with tempfile.TemporaryDirectory() as first_dir, \
                tempfile.TemporaryDirectory() as second_dir:
            first = asyncio.create_task(runner.start(first_dir))
            await entered.wait()
            second = asyncio.create_task(runner.start(second_dir))
            release.set()
            await first
            error = None
            try:
                await second
            except RuntimeError as exc:
                error = str(exc)
            check("the process cwd and recorded cwd stay identical",
                  state["cwd"] == os.path.realpath(first_dir)
                  and runner._workdir == state["cwd"], str(state))
            check("a concurrent different-cwd start is rejected",
                  error is not None and "close it before switching" in error,
                  str(error))
    finally:
        acp_mod.spawn_agent_process = original
        await runner.close()


async def test_acp_failure_falls_back_after_cleanup() -> None:
    print("\n[macbot.20] ACP startup and fallback are single-flight per agent")
    from leftover import agents as agents_mod

    state = {"starts": 0, "closed": 0}

    class FailingRunner(agents_mod.BaseRunner):
        def __init__(self, spec):
            super().__init__(spec)
            self.closed = False

        async def start(self, workdir: str) -> None:
            await super().start(workdir)
            state["starts"] += 1
            if state["starts"] == 1:
                await asyncio.sleep(0.03)
                raise ConnectionError("handshake failed")
            await asyncio.sleep(0.005)

        async def stream(self, prompt, on_event=None):
            await asyncio.sleep(0.05)
            if self.closed:
                yield agents_mod.Event("error", "closed")
            else:
                yield agents_mod.Event("text", "ACP")
                yield agents_mod.Event("done")

        async def close(self) -> None:
            state["closed"] += 1
            self.closed = True

    original = agents_mod.build_runner
    agents_mod.build_runner = lambda spec: FailingRunner(spec)
    spec = AgentSpec(
        key="fallback", label="Fallback", transport="acp",
        acp_command=["broken"], exec_command=[sys.executable,
        str(ROOT / "tests" / "fake_cli.py")], exec_output="json",
        exec_json_path="result", timeout=5)
    pool = agents_mod.AgentPool(Config(agents=[spec], default_workdir=str(ROOT)))
    try:
        turns = await asyncio.gather(
            pool.run(spec, "works one"), pool.run(spec, "works two"))
        check("only one cold ACP start is attempted before fallback",
              state["starts"] == 1, str(state))
        check("the failed runner is closed exactly once before replacement",
              state["closed"] == 1, str(state))
        check("both concurrent requests succeed on the managed fallback",
              all(turn.ok and "exec reply" in turn.text for turn in turns),
              repr([turn.short() for turn in turns]))
        check("the pool now owns the exec runner",
              isinstance(pool.peek(spec), agents_mod.ExecRunner))
    finally:
        agents_mod.build_runner = original
        await pool.shutdown()


def _stubborn_exec_spec(key: str, ready: Path) -> AgentSpec:
    script = (
        "import signal, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(sys.argv[1]).write_text('ready')\n"
        "time.sleep(60)\n"
    )
    return AgentSpec(
        key=key,
        label=key.title(),
        transport="exec",
        exec_command=[sys.executable, "-c", script, str(ready)],
        exec_output="text",
        timeout=30,
    )


async def _wait_for_exec_start(runner, ready: Path):
    deadline = asyncio.get_running_loop().time() + 2
    while not ready.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("test subprocess did not start")
        await asyncio.sleep(0.005)
    proc = runner._proc
    if proc is None:
        raise AssertionError("ready subprocess was not published on its runner")
    return proc


async def test_exec_external_cancel_reaps_process() -> None:
    print("\n[macbot.21] outer cancellation reaps an exec subprocess")
    from leftover.agents import exec_runner as exec_mod

    with tempfile.TemporaryDirectory() as tmp:
        ready = Path(tmp) / "ready"
        runner = exec_mod.ExecRunner(_stubborn_exec_spec("cancel-exec", ready))
        await runner.start(tmp)
        original_timeout = exec_mod._TERMINATE_TIMEOUT
        exec_mod._TERMINATE_TIMEOUT = 0.03
        task = asyncio.create_task(runner.run("prompt"))
        proc = None
        timed_out = False
        try:
            proc = await _wait_for_exec_start(runner, ready)
            try:
                await asyncio.wait_for(task, timeout=0.02)
            except asyncio.TimeoutError:
                timed_out = True
            check("wait_for propagates cancellation after cleanup",
                  timed_out and task.cancelled(),
                  f"timed_out={timed_out}, cancelled={task.cancelled()}")
            check("the cancelled subprocess has a final returncode",
                  proc.returncode is not None and runner._proc is None,
                  f"returncode={proc.returncode}, current={runner._proc}")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await runner.close()
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            exec_mod._TERMINATE_TIMEOUT = original_timeout


async def test_exec_pool_shutdown_reaps_process() -> None:
    print("\n[macbot.22] pool shutdown reaps an exec subprocess")
    from leftover import agents as agents_mod
    from leftover.agents import exec_runner as exec_mod

    with tempfile.TemporaryDirectory() as tmp:
        ready = Path(tmp) / "ready"
        spec = _stubborn_exec_spec("shutdown-exec", ready)
        pool = agents_mod.AgentPool(Config(
            agents=[spec], default_workdir=tmp, data_dir=tmp))
        original_timeout = exec_mod._TERMINATE_TIMEOUT
        exec_mod._TERMINATE_TIMEOUT = 0.03
        task = asyncio.create_task(pool.run(spec, "prompt"))
        runner = None
        proc = None
        try:
            deadline = asyncio.get_running_loop().time() + 2
            while runner is None:
                runner = pool.peek(spec)
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("pool did not publish its exec runner")
                if runner is None:
                    await asyncio.sleep(0.005)
            proc = await _wait_for_exec_start(runner, ready)
            await asyncio.wait_for(pool.shutdown(), timeout=0.3)
            await asyncio.wait_for(task, timeout=0.3)
            check("pool shutdown waits for the child to exit",
                  proc.returncode is not None,
                  f"returncode={proc.returncode}")
            check("pool shutdown clears the runner and process handle",
                  pool.peek(spec) is None and runner._proc is None)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await pool.shutdown()
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            exec_mod._TERMINATE_TIMEOUT = original_timeout


async def test_exec_structured_error_is_failure() -> None:
    print("\n[macbot.23] exit-zero structured CLI errors still fail the turn")
    from leftover.agents.exec_runner import ExecRunner

    spec = AgentSpec(
        key="json-error", label="JSON error", transport="exec",
        exec_command=[sys.executable, str(ROOT / "tests" / "fake_cli.py")],
        exec_output="json", exec_json_path="result",
        env={"FAKE_BEHAVIOR": "structured_error"}, timeout=5)
    runner = ExecRunner(spec)
    await runner.start(str(ROOT))
    turn = await runner.run("prompt")
    check("the JSON error message becomes Turn.error",
          turn.error == "Couldn't create session: Permission denied.",
          str(turn.error))
    check("exit zero cannot turn a structured error into success",
          not turn.ok and not turn.text, repr(turn.text))
    check("the raw structured payload remains available",
          turn.meta.get("code") == "FS_PERMISSION_DENIED", repr(turn.meta))
    check("normal exec completion clears the process handle",
          runner._proc is None)


async def test_debate_is_parallel_and_compact() -> None:
    print("\n[macbot.24] debate runs parallel sides with compact context")
    from leftover.orchestrator import Orchestrator, Plan
    from leftover.router import Router

    class DebatePool:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.prompts: list[tuple[str, str]] = []

        def peek(self, spec):
            return None

        async def run(self, spec, prompt, on_event=None):
            self.prompts.append((spec.key, prompt))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.05)
            finally:
                self.active -= 1
            marker = f"{spec.key.upper()}_ARGUMENT_" + "x" * 300
            return Turn(agent=spec, text=marker, seconds=0.05)

    agents = [
        AgentSpec(key="claude", label="Claude", persona="EDIT_FILES_NOW"),
        AgentSpec(key="gpt", label="Codex", persona="EDIT_FILES_NOW"),
        AgentSpec(key="cursor", label="Cursor", persona="EDIT_FILES_NOW"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            agents=agents, data_dir=tmp, debate_rounds=1,
            debate_turn_timeout=1,
            routing=Routing(strategy="order", order=[a.key for a in agents]),
        )
        pool = DebatePool()
        orch = Orchestrator(cfg, pool, Router(cfg, pool))
        topic = "Should compact debates ship?"
        started = asyncio.get_running_loop().time()
        turns = await orch.execute(
            Plan("debate", topic, agents, {"rounds": "1"}), None)
        elapsed = asyncio.get_running_loop().time() - started

    judge_prompt = next(prompt for key, prompt in pool.prompts
                        if key == "cursor")
    # Peak concurrency is the contract. Wall clock is only diagnostic here:
    # a loaded CI runner can take far longer while still overlapping.
    check("FOR and AGAINST execute concurrently",
          pool.max_active == 2,
          f"active={pool.max_active}, elapsed={elapsed:.3f}s")
    check("compact debate produces two arguments and one verdict",
          len(turns) == 3
          and [t.meta.get("discussion_role") for t in turns]
          == ["FOR", "AGAINST", "JUDGE"])
    check("the judge receives each argument exactly once",
          judge_prompt.count("CLAUDE_ARGUMENT_") == 1
          and judge_prompt.count("GPT_ARGUMENT_") == 1)
    check("the current proposition is not duplicated",
          all(prompt.count(topic) == 1 for _, prompt in pool.prompts))
    check("implementation personas are excluded from read-only debate",
          all("EDIT_FILES_NOW" not in prompt for _, prompt in pool.prompts))


async def test_heavy_is_parallel_leader_and_discuss() -> None:
    print("\n[macbot.24b] heavy runs parallel independent takes and compare-notes")
    from leftover.orchestrator import Orchestrator, Plan
    from leftover.router import Router

    class HeavyPool:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.phase_active = [0, 0]
            self.phase_peak = [0, 0]
            self.prompts: list[tuple[str, str]] = []

        def peek(self, spec):
            return None

        async def run(self, spec, prompt, on_event=None):
            self.prompts.append((spec.key, prompt))
            phase = 0 if "cannot see their answers yet" in prompt else 1
            self.active += 1
            self.phase_active[phase] += 1
            self.max_active = max(self.max_active, self.active)
            self.phase_peak[phase] = max(
                self.phase_peak[phase], self.phase_active[phase])
            try:
                await asyncio.sleep(0.05)
            finally:
                self.active -= 1
                self.phase_active[phase] -= 1
            marker = f"{spec.key.upper()}_TAKE_" + "x" * 80
            return Turn(agent=spec, text=marker, seconds=0.05)

    agents = [
        AgentSpec(key="grok", label="Grok", persona="EDIT_FILES_NOW"),
        AgentSpec(key="claude", label="Claude", persona="EDIT_FILES_NOW"),
        AgentSpec(key="gpt", label="Codex", persona="EDIT_FILES_NOW"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            agents=agents, data_dir=tmp, max_parallel=4,
            routing=Routing(strategy="order", order=[a.key for a in agents],
                            heavy_key="grok"),
        )
        pool = HeavyPool()
        orch = Orchestrator(cfg, pool, Router(cfg, pool))
        topic = "Should we split the worker?"
        started = asyncio.get_running_loop().time()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            turns = await orch.execute(
                Plan("heavy", topic, agents, {}), None)
        elapsed = asyncio.get_running_loop().time() - started

    independent = [p for _, p in pool.prompts
                   if "cannot see their answers yet" in p]
    discuss = [p for _, p in pool.prompts if "compare-notes round" in p]
    synthesis = [p for _, p in pool.prompts
                 if "give the practical conclusion" in p]
    check("independent takes and compare-notes both overlap",
          pool.phase_peak[0] == 3 and pool.phase_peak[1] == 3,
          f"peaks={pool.phase_peak}, elapsed={elapsed:.3f}s")
    check("orchestrator has no terminal UI side effect without an observer",
          stderr.getvalue() == "", repr(stderr.getvalue()))
    check("leader and workers then synthesis and discuss",
          len(turns) == 6
          and [t.meta.get("discussion_role") for t in turns]
          == ["LEADER", "WORKER", "WORKER", "SYNTHESIS", "DISCUSS", "DISCUSS"])
    check("independent takes cannot see each other",
          len(independent) == 3
          and all("GROK_TAKE_" not in p and "CLAUDE_TAKE_" not in p
                  and "GPT_TAKE_" not in p for p in independent))
    check("the leader synthesizes from every independent take",
          len(synthesis) == 1
          and synthesis[0].count("GROK_TAKE_") == 1
          and synthesis[0].count("CLAUDE_TAKE_") == 1
          and synthesis[0].count("GPT_TAKE_") == 1)
    check("workers compare notes from the independent takes",
          len(discuss) == 2
          and all(p.count("GROK_TAKE_") == 1 and p.count("CLAUDE_TAKE_") == 1
                  and p.count("GPT_TAKE_") == 1 for p in discuss))
    check("the current topic is not duplicated",
          all(prompt.count(topic) == 1 for _, prompt in pool.prompts))
    check("only the leader synthesis may implement",
          all("EDIT_FILES_NOW" not in p for p in independent + discuss)
          and all("Do not edit files" in p for p in independent + discuss)
          and len(synthesis) == 1 and "EDIT_FILES_NOW" in synthesis[0]
          and "make the change in the working directory" in synthesis[0])

    observed_pool = HeavyPool()
    observed_orch = Orchestrator(
        cfg, observed_pool, Router(cfg, observed_pool))
    progress_log = io.StringIO()
    view = ui_mod.Roster(
        mode="heavy", out=progress_log, heartbeat_seconds=0)
    await observed_orch.execute(
        Plan("heavy", topic, agents, {}), None, progress=view)
    rendered = progress_log.getvalue()
    check("heavy observer receives both phases and every live role",
          "phase 1/2 · independent" in rendered
          and "phase 2/2 · compare-notes" in rendered
          and "leader" in rendered and "worker" in rendered
          and "synthesis" in rendered and "discuss" in rendered
          and rendered.count("3/3 finished") >= 2,
          rendered)


async def test_pool_lifecycle_events_are_structured() -> None:
    print("\n[macbot.24c] pool publishes queued, preparing, and running")
    from leftover import agents as agents_mod

    class LifecycleRunner(BaseRunner):
        async def run(self, prompt: str, on_event=None) -> Turn:
            if on_event is not None:
                await on_event(Event("text", "READY"))
                await on_event(Event("done"))
            return Turn(agent=self.spec, text="READY")

    original_build = agents_mod.build_runner
    agents_mod.build_runner = lambda spec: LifecycleRunner(spec)
    spec = AgentSpec(key="lifecycle", label="Lifecycle")
    events: list[Event] = []

    async def collect(event: Event) -> None:
        events.append(event)

    collect.leftover_lifecycle = True  # type: ignore[attr-defined]

    with tempfile.TemporaryDirectory() as tmp:
        pool = agents_mod.AgentPool(Config(
            agents=[spec], data_dir=tmp, default_workdir=tmp))
        try:
            turn = await pool.run(spec, "work", collect)
        finally:
            await pool.shutdown()
            agents_mod.build_runner = original_build
    lifecycle = [event for event in events if event.kind == "lifecycle"]
    check("lifecycle order follows the real pool boundary",
          [event.text for event in lifecycle]
          == ["queued", "preparing", "running"],
          repr([(event.kind, event.text) for event in events]))
    check("lifecycle events include a stable turn id and structured state",
          turn.ok and len({event.data.get("turn_id") for event in lifecycle}) == 1
          and all(event.data.get("state") == event.text for event in lifecycle)
          and bool(lifecycle[0].data.get("turn_id")),
          repr([event.data for event in lifecycle]))


async def test_event_sink_obeys_the_turn_deadline() -> None:
    print("\n[macbot.25] event delivery is part of the turn deadline")
    from leftover.orchestrator import Orchestrator
    from leftover.router import Router

    class OneEventRunner(BaseRunner):
        async def stream(self, prompt: str, on_event=None):
            yield Event("text", "READY")
            yield Event("done")

    spec = AgentSpec(
        key="sink", label="Sink", exec_command=["unused"], timeout=0.03)
    runner = OneEventRunner(spec)
    cancelled = False

    async def stuck_sink(event: Event) -> None:
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    began = time.monotonic()
    turn = await runner.run("smoke", stuck_sink)
    elapsed = time.monotonic() - began
    await asyncio.sleep(0)
    check("a stuck event sink cannot hold the routed turn open",
          elapsed < 0.15 and cancelled,
          f"elapsed={elapsed:.3f}s, cancelled={cancelled}")
    check("sink timeout is explicit and keeps partial text",
          turn.meta.get("timeout_kind") == "sink"
          and turn.text == "READY" and turn.error is not None,
          repr(turn))
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(agents=[spec], data_dir=tmp)
        router = Router(cfg, object())
        failure = router.observe(spec, turn)
        terminal = router._terminal_timeout(turn)
    check("delivery timeout stops replay without penalizing the backend",
          failure is not None and terminal
          and router.h(spec).consecutive == 0,
          f"failure={failure}, health={router.h(spec).consecutive}")

    class NeverRunPool:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, spec, prompt, on_event=None):
            self.calls += 1
            return Turn(agent=spec, text="should not run")

    async def stuck_factory(_spec):
        await asyncio.Event().wait()

    with tempfile.TemporaryDirectory() as tmp:
        pool = NeverRunPool()
        cfg = Config(
            agents=[spec], data_dir=tmp,
            routing=Routing(
                strategy="order", order=[spec.key], event_sink_timeout=0.03))
        router = Router(cfg, pool)
        began = time.monotonic()
        routed, decision = await router.run(
            lambda _spec: "prompt", primary=spec, sink=stuck_factory,
            ordered_chain=[spec])
        factory_elapsed = time.monotonic() - began
        buffered = Turn(agent=spec, text="BUFFERED")
        orch = Orchestrator(cfg, pool, router)
        began = time.monotonic()
        await orch._emit_turn(buffered, stuck_factory)
        buffered_elapsed = time.monotonic() - began
    check("sink creation is bounded before an agent starts",
          factory_elapsed < 0.15 and pool.calls == 0
          and routed.meta.get("timeout_kind") == "sink"
          and len(decision.attempts) == 1,
          f"elapsed={factory_elapsed:.3f}s, calls={pool.calls}")
    check("buffered group delivery also has a finite deadline",
          buffered_elapsed < 0.15 and "delivery_error" in buffered.meta,
          f"elapsed={buffered_elapsed:.3f}s, meta={buffered.meta}")

    from leftover.orchestrator import summarise
    summary = summarise([buffered])
    check("group summaries expose delivery failure without discarding text",
          buffered.ok and buffered.text == "BUFFERED"
          and "delivery failed: timed out after 0.03s" in summary,
          summary)


async def test_stream_sink_keeps_sync_io_off_the_loop() -> None:
    print("\n[macbot.26] synchronous terminal output is isolated and cancellable")

    class BlockingOutput:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()
            self.parts: list[str] = []
            self._block_next = True

        def write(self, text: str) -> int:
            if self._block_next:
                self._block_next = False
                self.entered.set()
                self.release.wait(timeout=1)
            self.parts.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    async def wait_until(predicate, timeout: float = 0.2) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("condition was not reached")
            await asyncio.sleep(0.002)

    blocked_out = BlockingOutput()
    blocked_sink = ui_mod.StreamSink("Blocked", out=blocked_out)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.003)

    ticker_task = asyncio.create_task(ticker())
    blocked_task = asyncio.create_task(
        blocked_sink(Event("text", "BLOCKED")))
    await wait_until(blocked_out.entered.is_set)
    await asyncio.sleep(0.025)
    cancel_started = asyncio.get_running_loop().time()
    blocked_task.cancel()
    await asyncio.gather(blocked_task, return_exceptions=True)
    cancel_elapsed = asyncio.get_running_loop().time() - cancel_started
    blocked_out.release.set()
    await asyncio.sleep(0.02)
    ticker_task.cancel()
    await asyncio.gather(ticker_task, return_exceptions=True)
    check("a blocking TextIO write does not freeze the event loop",
          ticks >= 5, f"ticks={ticks}")
    check("cancelling a blocked output await returns promptly",
          cancel_elapsed < 0.05, f"elapsed={cancel_elapsed:.3f}s")

    queued_out = BlockingOutput()
    queued_sink = ui_mod.StreamSink("Queued", out=queued_out)
    first = asyncio.create_task(queued_sink(Event("text", "FIRST")))
    await wait_until(queued_out.entered.is_set)
    second = asyncio.create_task(queued_sink(Event("text", "SECOND")))
    await wait_until(lambda: queued_sink._writer.queue.qsize() == 1)
    second.cancel()
    await asyncio.gather(second, return_exceptions=True)
    queued_out.release.set()
    await asyncio.wait_for(first, timeout=0.3)
    await asyncio.sleep(0.02)
    rendered = "".join(queued_out.parts)
    check("cancelled queued output is never written later",
          "FIRST" in rendered and "SECOND" not in rendered,
          repr(rendered))

    fanout_outputs = [BlockingOutput() for _ in range(20)]
    fanout_sinks = [
        ui_mod.StreamSink(f"Fanout {index}", out=out)
        for index, out in enumerate(fanout_outputs)
    ]
    fanout_tasks = [
        asyncio.create_task(sink(Event("text", f"VALUE {index}")))
        for index, sink in enumerate(fanout_sinks)
    ]
    await wait_until(lambda: any(out.entered.is_set()
                                 for out in fanout_outputs))
    await asyncio.sleep(0.02)
    writer_threads = [
        thread for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith("leftover-stream-")
    ]
    for task in fanout_tasks:
        task.cancel()
    await asyncio.gather(*fanout_tasks, return_exceptions=True)
    for out in fanout_outputs:
        out.release.set()
    await asyncio.sleep(0.03)
    check("many blocked sinks share one bounded daemon writer",
          len(writer_threads) <= 1,
          repr([thread.name for thread in writer_threads]))


async def test_roster_keeps_sync_io_off_the_loop() -> None:
    print("\n[macbot.26a] Roster output is ordered, bounded, and flushable")

    class BlockingTTY(io.StringIO):
        def __init__(self, shared: list[tuple[str, str]]) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.parts: list[str] = []
            self.shared = shared
            self._block_next = True

        def isatty(self) -> bool:
            return True

        def write(self, text: str) -> int:
            if self._block_next:
                self._block_next = False
                self.entered.set()
                self.release.wait(timeout=1)
            self.parts.append(text)
            self.shared.append(("roster", text))
            return super().write(text)

        def flush(self) -> None:
            return None

    class AnswerOutput(io.StringIO):
        def __init__(self, shared: list[tuple[str, str]]) -> None:
            super().__init__()
            self.shared = shared

        def write(self, text: str) -> int:
            self.shared.append(("answer", text))
            return super().write(text)

    async def wait_until(predicate, timeout: float = 0.2) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("condition was not reached")
            await asyncio.sleep(0.002)

    spec = AgentSpec(key="gpt", label="Codex", emoji="G")

    snapshot_out = io.StringIO()
    snapshot_out.isatty = lambda: True  # type: ignore[attr-defined]
    snapshot = ui_mod.Roster(
        mode="broadcast", out=snapshot_out, width=72,
        heartbeat_seconds=0, close_timeout=0.2)
    await snapshot.begin_phase(
        mode="broadcast", title="independent answers", index=1, total=1,
        seats=[(spec, "member")], parallel=True)
    await asyncio.sleep(0.02)
    snapshot_events = await snapshot.sink(spec, "member")(spec)
    await snapshot_events(Event("status", "checking routes"))
    await snapshot.finish(
        spec, Turn(agent=spec, text="READY", seconds=2.0), "member")
    snapshot_flushed = await snapshot.end_phase()
    ordered = snapshot_out.getvalue()

    pty_master, pty_slave = os.openpty()
    pty_out = os.fdopen(os.dup(pty_slave), "w", buffering=1)
    pty_roster = ui_mod.Roster(
        mode="broadcast", out=pty_out, width=72,
        heartbeat_seconds=0, close_timeout=0.2)
    await pty_roster.begin_phase(
        mode="broadcast", title="independent answers", index=1, total=1,
        seats=[(spec, "member")], parallel=True)
    pty_events = await pty_roster.sink(spec, "member")(spec)
    await pty_events(Event("tool", "inspect routes"))
    await pty_roster.finish(
        spec, Turn(agent=spec, text="READY", seconds=1.0), "member")
    await pty_roster.end_phase()
    os.set_blocking(pty_master, False)
    pty_parts: list[bytes] = []
    while True:
        try:
            part = os.read(pty_master, 4096)
        except (BlockingIOError, OSError):
            break
        if not part:
            break
        pty_parts.append(part)
    pty_out.close()
    os.close(pty_slave)
    os.close(pty_master)
    pty_text = b"".join(pty_parts).decode("utf-8", errors="replace")

    shared_output: list[tuple[str, str]] = []
    out = BlockingTTY(shared_output)
    roster = ui_mod.Roster(
        mode="broadcast", out=out, width=72, heartbeat_seconds=0,
        close_timeout=0.08)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.003)

    ticker_task = asyncio.create_task(ticker())
    began = asyncio.get_running_loop().time()
    await roster.begin_phase(
        mode="broadcast",
        title="\033[3A\033[Jindependent answers\rspoof",
        index=1, total=1,
        seats=[(spec, "member")], parallel=True)
    begin_elapsed = asyncio.get_running_loop().time() - began
    await wait_until(out.entered.is_set)

    answer_out = AnswerOutput(shared_output)
    answer_sink = ui_mod.StreamSink(
        "Healthy", out=answer_out, show_header=False)
    answer_started = asyncio.get_running_loop().time()
    answer_delivered = True
    try:
        await asyncio.wait_for(
            answer_sink(Event("text", "ANSWER")), timeout=0.2)
        await asyncio.wait_for(answer_sink(Event("done")), timeout=0.2)
    except (TimeoutError, asyncio.TimeoutError):
        answer_delivered = False
    answer_elapsed = asyncio.get_running_loop().time() - answer_started

    on_event = await roster.sink(spec, "member")(spec)
    for index in range(64):
        await on_event(Event("status", f"step {index}"))
    await roster.finish(
        spec, Turn(agent=spec, text="READY", seconds=2.0), "member")
    pending_writes = len(roster._writes)

    close_started = asyncio.get_running_loop().time()
    close_task = asyncio.create_task(roster.end_phase())
    await asyncio.wait_for(close_task, timeout=0.3)
    close_elapsed = asyncio.get_running_loop().time() - close_started
    closed_lines = roster.lines()
    await on_event(Event("status", "late update after close"))
    await roster.finish(
        spec, Turn(agent=spec, text="LATE", seconds=9.0), "member")
    roster.mark(spec, "failed", "late mark after close")
    late_update_ignored = (
        roster.lines() == closed_lines
        and not roster._writes and roster._write_task is None)
    responsive_ticks = ticks
    out.release.set()
    await wait_until(
        lambda: any(owner == "roster" for owner, _text in shared_output))
    shared_text = "".join(text for _owner, text in shared_output)
    after_answer = shared_text.partition("ANSWER\n")[2]

    await roster.begin_phase(
        mode="broadcast", title="next phase", index=1, total=1,
        seats=[(spec, "member")], parallel=True)
    await on_event(Event("tool", "stale update after reopen"))
    reopened_events = await roster.sink(spec, "member")(spec)
    await reopened_events(Event(
        "tool", "\033[9A\033[Jfresh update after reopen\x07"))
    await roster.finish(
        spec, Turn(agent=spec, text="READY", seconds=1.0), "member")
    await roster.end_phase()
    reopened_text = "".join(out.parts)

    ticker_task.cancel()
    await asyncio.gather(ticker_task, return_exceptions=True)
    cursor = ordered.find("\033[3A\033[J")
    queued = ordered.find("queued")
    done = ordered.find("done", max(0, cursor))
    check("a blocked roster write does not stall the event loop",
          begin_elapsed < 0.05 and responsive_ticks >= 5,
          f"begin={begin_elapsed:.3f}s, ticks={responsive_ticks}")
    check("blocked roster output cannot delay or drop streamed answers",
          answer_delivered and answer_elapsed < 0.2
          and answer_out.getvalue() == "ANSWER\n",
          (f"delivered={answer_delivered}, elapsed={answer_elapsed:.3f}s, "
           f"output={answer_out.getvalue()!r}"))
    check("late generic TextIO output cannot erase a delivered answer",
          not roster._tty and bool(after_answer) and "\033[" not in after_answer
          and "\r" not in after_answer,
          repr(shared_output))
    check("a real POSIX TTY uses append-only progress without cursor control",
          not pty_roster._tty and "\033[" not in pty_text
          and "started" in pty_text and "complete" in pty_text,
          repr(pty_text))
    check("rapid roster snapshots stay bounded while output is blocked",
          pending_writes <= 2,
          f"pending={pending_writes}, limit={roster._WRITE_QUEUE_SIZE}")
    check("a permanently blocked roster close returns at its deadline",
          close_task.done() and close_elapsed < 0.2,
          f"elapsed={close_elapsed:.3f}s")
    check("closed rosters reject late events without restarting the pump",
          late_update_ignored,
          f"writes={list(roster._writes)!r}, task={roster._write_task!r}")
    check("a reopened phase rejects callbacks from the prior epoch",
          "stale update after reopen" not in reopened_text
          and "fresh update after reopen" in reopened_text
          and "\033[" not in reopened_text and "\x07" not in reopened_text,
          repr(reopened_text))
    check("the exact in-memory TTY flushes initial, final, then freeze in order",
          snapshot_flushed is None and ordered.endswith("\n\n")
          and queued >= 0 and cursor > queued and done > cursor,
          repr(ordered))


async def test_stream_sink_shows_plan_and_thought() -> None:
    print("\n[macbot.26b] StreamSink prints compact plan/thought, not just tools")
    buf = io.StringIO()
    sink = ui_mod.StreamSink("Codex", out=buf, show_header=False)
    await sink(Event("thought", "looking at leftover progress rendering"))
    await sink(Event("status", "surface the in-progress plan step"))
    await sink(Event("tool", "Read File leftover/macbot.py"))
    await sink(Event("done"))
    deadline = asyncio.get_running_loop().time() + 0.3
    out = ""
    while asyncio.get_running_loop().time() < deadline:
        out = buf.getvalue()
        if ("looking at leftover progress rendering" in out
                and "surface the in-progress plan step" in out
                and "Read File leftover/macbot.py" in out):
            break
        await asyncio.sleep(0.01)
    check("thought becomes a dim status line",
          "· looking at leftover progress rendering" in out, repr(out))
    check("plan status is visible next to tools",
          "· surface the in-progress plan step" in out
          and "▸ Read File leftover/macbot.py" in out, repr(out))
    check("activity is not treated as answer text without a bullet",
          not out.lstrip().startswith("looking at leftover"), repr(out))


def test_usher_ux_surface() -> None:
    print("\n[macbot.12] usher UX: doctor, seat, timeout")
    from leftover import doctor as doctor_mod
    from leftover import macbot as macbot_mod

    check("90s is 90 seconds", parse_duration("90s") == 90)
    check("2m is 120 seconds", parse_duration("2m") == 120)
    check("5m30s adds", parse_duration("5m30s") == 330)
    try:
        parse_duration("nope")
        rejected = False
    except Exception:
        rejected = True
    check("bad duration is rejected", rejected)
    seat = ui_mod.seat_line("Codex", "coding", reason="lag+waste")
    check("compact activity collapses whitespace and truncates",
          ui_mod.compact_activity("  looking\n  at   leftover  ")
          == "looking at leftover"
          and ui_mod.compact_activity("x" * 130).endswith(" ...")
          and len(ui_mod.compact_activity("x" * 130)) == 124)
    check("seat line is usher-shaped, lag+waste not reputation",
          seat.startswith("→ Codex") and "lag+waste" in seat
          and "override with @name" in seat and "strength" not in seat, seat)
    check("headless seat drops the override hint",
          "headless" in ui_mod.seat_line("Codex", "coding", headless=True)
          and "override" not in ui_mod.seat_line(
              "Codex", "coding", headless=True))
    fail = ui_mod.failover_line("Codex", "Grok", failure_kind="quota")
    check("quota failover names the cap",
          "hit its cap" in fail and "failing over to Grok" in fail
          and "continuation notice" in fail, fail)

    missing = AgentSpec(key="gpt", label="Codex",
                        interactive_command=["macbot-not-installed-xyz"])
    roster = asyncio.run(doctor_mod.run(Config(agents=[missing], source_path=""), {}))
    check("doctor roster lists missing CLIs with an install hint",
          roster.startswith("leftover doctor")
          and "not installed" in roster
          and "npm install -g @openai/codex" in roster
          and "config:" in roster and "ledger:" in roster, roster)
    present = AgentSpec(key="gpt", label="Codex", interactive_command=["true"])
    cached = {
        "gpt": Quota(
            agent="gpt",
            windows=[Window(name="5h", used_percent=20.0, source=REPORTED)],
        ).to_dict()
    }
    roster = asyncio.run(doctor_mod.run(Config(agents=[present]), cached))
    check("doctor remaining bar uses cached reported remaining",
          "80%" in roster and "remaining" in roster and "5h" in roster, roster)
    extra_cached = {
        "gpt": Quota(
            agent="gpt",
            windows=[
                Window(name="5h", used_percent=0.0, source=REPORTED),
                Window(name="extra", used_percent=93.0, source=REPORTED),
            ],
        ).to_dict()
    }
    roster = asyncio.run(doctor_mod.run(Config(agents=[present]), extra_cached))
    check("doctor keeps the plan bar and suffixes extra remaining",
          "100%" in roster and "5h" in roster and "extra" in roster
          and "7%" in roster, roster)
    estimated_cached = {
        "gpt": Quota(
            agent="gpt",
            windows=[Window(name="5h budget", used_percent=40.0,
                            source=ESTIMATED)],
        ).to_dict()
    }
    roster = asyncio.run(doctor_mod.run(
        Config(agents=[present]), estimated_cached))
    check("doctor remaining uses estimated percent when vendor is silent",
          "60%" in roster and "5h budget" in roster, roster)

    original_load = macbot_mod.config_mod.load
    macbot_mod.config_mod.load = lambda _path: Config(agents=[present])
    try:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = macbot_mod.main(["--timeout", "2m", "fix tests"])
        check("--timeout without -p is an error",
              code == 2 and "requires --print" in stderr.getvalue(),
              stderr.getvalue())
        ns = macbot_mod._parse_argv(["-p", "--timeout", "90s", "fix"])
        check("-p --timeout 90s parses",
              ns.headless and ns.timeout == 90 and ns.prompt == ["fix"])
    finally:
        macbot_mod.config_mod.load = original_load


def test_subcommand_config_flag() -> None:
    print("\n[macbot.12b] `leftover doctor|quota --config` reaches the loader")
    from leftover import macbot as macbot_mod

    for flag in ("--config", "-c"):
        ns = macbot_mod._parse_argv(["doctor", flag, "/tmp/leftover-test.toml"])
        check(f"doctor {flag} PATH is kept",
              ns.command == "doctor" and ns.config == "/tmp/leftover-test.toml",
              repr(ns.config))
    ns = macbot_mod._parse_argv(["quota", "--config", "/tmp/x.toml"])
    check("quota --config PATH is kept", ns.config == "/tmp/x.toml")
    ns = macbot_mod._parse_argv(["quota", "--json", "--config", "/tmp/x.toml"])
    check("quota --json --config is kept",
          ns.command == "quota" and ns.json and ns.config == "/tmp/x.toml")
    ns = macbot_mod._parse_argv(["quota", "--config", "/tmp/x.toml", "--json"])
    check("quota --config --json either order", ns.json and ns.config == "/tmp/x.toml")
    try:
        macbot_mod._parse_argv(["quota", "--config"])
        clean = False
    except SystemExit as exc:
        clean = "needs a path" in str(exc)
    except Exception:
        clean = False
    check("a missing value is a usage error, not a traceback", clean)


async def test_print_json_envelope_and_stdin() -> None:
    print("\n[macbot.13] --print --json envelope and stdin replay")
    from leftover import agents as agents_mod

    class JsonPool:
        prompts: list[str] = []

        def __init__(self, config: Config) -> None:
            self.config = config

        async def set_workdir(self, path: str) -> None:
            return None

        def peek(self, spec: AgentSpec):
            return None

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            type(self).prompts.append(prompt)
            return Turn(agent=spec, text="PATCHED")

        async def shutdown(self) -> None:
            return None

    spec = AgentSpec(key="gpt", label="Codex", interactive_command=["true"])
    original = agents_mod.AgentPool
    agents_mod.AgentPool = JsonPool
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(agents=[spec], data_dir=tmp,
                         routing=Routing(strategy="order", order=["gpt"],
                                         coding_keys=["gpt"]))
            pick = Pick(spec, ["gpt"], {}, "test", "coding", "fix tests")
            JsonPool.prompts = []
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                code = await run_print(
                    cfg, pick, as_json=True, stdin_extra="FILE.txt")
            try:
                blob = json.loads(stdout.getvalue())
            except json.JSONDecodeError:
                blob = {}
            check("--print --json is one envelope on stdout",
                  code == 0 and blob.get("agent") == "gpt"
                  and blob.get("kind") == "coding"
                  and blob.get("exit_code") == 0
                  and blob.get("output") == "PATCHED"
                  and blob.get("attempts")
                  and blob["attempts"][0]["agent"] == "gpt",
                  repr(stdout.getvalue()))
            check("piped stdin is replayed into the prompt",
                  JsonPool.prompts and "--- stdin ---" in JsonPool.prompts[0]
                  and "FILE.txt" in JsonPool.prompts[0],
                  str(JsonPool.prompts[:1]))
            check("json envelope does not leak the answer as raw stdout",
                  stdout.getvalue().strip().startswith("{"),
                  repr(stdout.getvalue()))
    finally:
        agents_mod.AgentPool = original


def test_piped_stdin_read_has_a_hard_boundary() -> None:
    print("\n[macbot.13a] piped stdin does not wait forever for EOF")
    from leftover.macbot import read_piped_stdin

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"partial input")
    reader = os.fdopen(read_fd, "r", encoding="utf-8")
    original_stdin = sys.stdin
    result: dict[str, object] = {}

    def read_open_pipe() -> None:
        started = time.monotonic()
        result["text"] = read_piped_stdin()
        result["elapsed"] = time.monotonic() - started

    try:
        sys.stdin = reader
        thread = threading.Thread(target=read_open_pipe, daemon=True)
        thread.start()
        thread.join(timeout=0.4)
        returned_while_open = not thread.is_alive()
        if not returned_while_open:
            os.close(write_fd)
            write_fd = -1
            thread.join(timeout=1.0)
        check("partial pipe data returns while its writer remains open",
              returned_while_open
              and result.get("text") == "partial input"
              and float(result.get("elapsed") or 99) < 0.3,
              repr(result))
    finally:
        sys.stdin = original_stdin
        reader.close()
        if write_fd >= 0:
            os.close(write_fd)

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"complete input\n")
    os.close(write_fd)
    reader = os.fdopen(read_fd, "r", encoding="utf-8")
    try:
        sys.stdin = reader
        complete = read_piped_stdin()
    finally:
        sys.stdin = original_stdin
        reader.close()
    check("ordinary EOF input is still read completely",
          complete == "complete input\n", repr(complete))


def main() -> int:
    test_compose_followup_is_bare()
    test_intent()
    test_repl_completes_commands_and_mentions()
    test_roster_is_per_agent_status_not_logos()
    test_roster_tty_snapshots_cover_terminal_states()
    test_score_short_window_beats_fat_monthly()
    test_score_depleted_short_window_loses()
    test_score_fresh_short_window_does_not_starve_overdue_weekly()
    asyncio.run(test_route_respects_ahead_weekly_window())
    test_score_allocation_window_outranks_rotting_session()
    test_score_session_ahead_does_not_gate_behind_weekly()
    asyncio.run(test_route_skips_full_session_window())
    asyncio.run(test_sticky_requires_a_live_session())
    asyncio.run(test_pick_plan_and_cu())
    asyncio.run(test_pick_heavy_is_local_multi_model_collab())
    test_why_table_is_lag_waste_not_reputation()
    test_usher_ux_surface()
    test_subcommand_config_flag()
    asyncio.run(test_print_json_envelope_and_stdin())
    test_piped_stdin_read_has_a_hard_boundary()
    test_agent_is_identity_not_mention()
    test_cli_routing_progress_is_human_only()
    asyncio.run(test_routing_progress_stops_after_cancel())
    test_skill_install_is_symlink()
    test_skill_scope_toggles_vendor_cli_influence()
    test_skill_scope_migrates_owned_legacy_paths()
    test_skill_scope_respects_cli_config_roots()
    test_pick_rechecks_scope_before_publishing_handoff()
    test_spawned_cli_gets_leftover_self()
    test_quota_serde()
    asyncio.run(test_quota_disk_cache())
    asyncio.run(test_run_print_uses_current_workdir())
    asyncio.run(test_run_print_respects_pick_chain())
    asyncio.run(test_progress_is_visible_and_output_stays_clean())
    asyncio.run(test_print_long_running_tool_does_not_exit_124())
    asyncio.run(test_group_routes_survive_cli_handoff())
    test_builtin_acp_commands()
    test_antigravity_is_exec_only_and_stays_first_party()
    asyncio.run(test_acp_start_failure_closes_transport())
    asyncio.run(test_acp_concurrent_start_is_singleton())
    asyncio.run(test_acp_cancel_then_prompt_ignores_stale_done())
    asyncio.run(test_acp_cancel_rpc_is_bounded())
    asyncio.run(test_acp_forced_restart_drops_late_updates())
    asyncio.run(test_acp_sink_error_closes_stream())
    asyncio.run(test_acp_concurrent_workdirs_do_not_cross())
    asyncio.run(test_acp_failure_falls_back_after_cleanup())
    asyncio.run(test_exec_external_cancel_reaps_process())
    asyncio.run(test_exec_pool_shutdown_reaps_process())
    asyncio.run(test_exec_structured_error_is_failure())
    asyncio.run(test_debate_is_parallel_and_compact())
    asyncio.run(test_heavy_is_parallel_leader_and_discuss())
    asyncio.run(test_pool_lifecycle_events_are_structured())
    asyncio.run(test_event_sink_obeys_the_turn_deadline())
    asyncio.run(test_stream_sink_keeps_sync_io_off_the_loop())
    asyncio.run(test_roster_keeps_sync_io_off_the_loop())
    asyncio.run(test_stream_sink_shows_plan_and_thought())
    ok = all(RESULTS)
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
