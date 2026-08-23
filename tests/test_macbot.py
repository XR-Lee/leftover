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
    prepare_task, run_argv, run_print, run_discuss, save_state)
from leftover.agents.base import BaseRunner, Event, Turn             # noqa: E402
from leftover.quota import Quota, Window, REPORTED, ESTIMATED        # noqa: E402
from leftover.score import AgentScore, WindowScore, score_quota      # noqa: E402

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
    check("follow-up is just the user line", later == "add a test")
    plan = _compose(spec, "split worker", Transcript(), followup=False, kind="plan")
    check("plan turn forbids edits", "Plan only" in plan)
    check("plan worker must not re-enter macbot", "Do not run leftover" in plan)


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


def test_score_short_window_beats_fat_monthly() -> None:
    print("\n[macbot.2] waste prefers a 5h window about to reset")
    now = 1_000_000.0
    codex = Quota(agent="gpt", windows=[Window(
        name="5h", used_percent=20.0, resets_at=now + 1800, source=REPORTED)])
    cursor = Quota(agent="cursor", windows=[Window(
        name="monthly", used_percent=10.0, resets_at=now + 20 * 86400,
        source=ESTIMATED)])
    s_gpt = score_quota("gpt", codex, now=now)
    s_cur = score_quota("cursor", cursor, now=now)
    check("codex total > cursor total", s_gpt.total > s_cur.total,
          f"gpt {s_gpt.total:.3f} vs cursor {s_cur.total:.3f}")
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


async def test_sticky_requires_a_live_session() -> None:
    print("\n[macbot.3c] persisted cwd choice cannot override fresh quota ranking")
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
    from leftover.macbot import _skill_source, link_skill, skill_destinations
    dests = [str(path) for path in skill_destinations()]
    check("leftover skill is installed",
          any("/skills/leftover/SKILL.md" in path for path in dests))
    check("the legacy CLI does not duplicate the product skill",
          len(dests) == 5
          and not any("/skills/macbot/SKILL.md" in path for path in dests),
          repr(dests))
    skill = _skill_source().read_text()
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
                await on_event(Event("tool", "inspect repository"))
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
                  and "leftover: Codex tool: inspect repository" in progress,
                  repr(progress))
            check("--print emits a heartbeat during a quiet interval",
                  "leftover: still working (Codex)" in progress,
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
                  "leftover: still working (Codex)" in stderr.getvalue(),
                  repr(stderr.getvalue()))

            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(stderr):
                ok = await _discuss(
                    cfg, router, Transcript(),
                    intent_mod.parse("/rt @gpt @grok compare"),
                    heartbeat_seconds=0.01)
            check("group discussions heartbeat during quiet agents",
                  ok and "leftover: still working" in stderr.getvalue(),
                  repr(stderr.getvalue()))
    finally:
        agents_mod.AgentPool = original


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
    test_score_short_window_beats_fat_monthly()
    test_score_depleted_short_window_loses()
    test_score_fresh_short_window_does_not_starve_overdue_weekly()
    asyncio.run(test_sticky_requires_a_live_session())
    asyncio.run(test_pick_plan_and_cu())
    test_why_table_is_lag_waste_not_reputation()
    test_usher_ux_surface()
    test_subcommand_config_flag()
    asyncio.run(test_print_json_envelope_and_stdin())
    test_piped_stdin_read_has_a_hard_boundary()
    test_agent_is_identity_not_mention()
    test_cli_routing_progress_is_human_only()
    asyncio.run(test_routing_progress_stops_after_cancel())
    test_skill_install_is_symlink()
    test_quota_serde()
    asyncio.run(test_quota_disk_cache())
    asyncio.run(test_run_print_uses_current_workdir())
    asyncio.run(test_run_print_respects_pick_chain())
    asyncio.run(test_progress_is_visible_and_output_stays_clean())
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
    asyncio.run(test_event_sink_obeys_the_turn_deadline())
    asyncio.run(test_stream_sink_keeps_sync_io_off_the_loop())
    ok = all(RESULTS)
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
