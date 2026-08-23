"""Quota parsing, health state machine and automatic fallback."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leftover import quota as q                                   # noqa: E402
from leftover import rhythm as rh                                 # noqa: E402
from leftover.agents import AgentPool, Turn                       # noqa: E402
from leftover.config import AgentSpec, Config, Routing, load      # noqa: E402
from leftover.orchestrator import Orchestrator, Plan              # noqa: E402
from leftover.router import CONTINUATION_GUARD, Router, State     # noqa: E402

MOCK = str(ROOT / "tests" / "mock_acp_agent.py")
RESULTS: list[bool] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  ' + detail if detail else ''}")


def agent(key: str, behavior: str = "ok", **kw) -> AgentSpec:
    return AgentSpec(key=key, label=key.title(), emoji=key[0].upper(),
                     transport="acp", acp_command=[sys.executable, MOCK],
                     env={"MOCK_BEHAVIOR": behavior}, timeout=25, **kw)


def make(agents: list[AgentSpec], tmp: str, **routing) -> tuple[Config, Router]:
    cfg = Config(agents=agents, data_dir=tmp,
                 routing=Routing(order=[a.key for a in agents], **routing))
    return cfg, Router(cfg, AgentPool(cfg))


# --------------------------------------------------------------------------

def test_classification() -> None:
    print("\n[1] refusal messages -> actionable failures")
    cases = [
        ("You've hit your weekly limit · resets Mon 12:00am", "quota", "weekly", True),
        ("You've hit your session limit · resets 3:45pm", "quota", "5h", True),
        ("You've hit your Opus limit · resets 3:45pm", "quota", "model", True),
        ("spend limit reached (daily; resets 2027-08-09 00:00 UTC)", "quota", "spend", True),
        ("spend limit reached (daily; resets 2020-01-01 00:00 UTC)", "quota", "spend", False),
        ("You've hit your usage limit", "quota", "plan", False),
        ("API Error: Request rejected (429)", "rate_limit", "", False),
        ("Please log in to continue", "auth", "", False),
        ("connection reset by peer", "transient", "", False),
        ("Here is the answer to your question.", None, "", False),
    ]
    for text, kind, window, has_reset in cases:
        f = q.classify(text)
        got = f.kind if f else None
        ok = got == kind and (not kind or f.window == window)
        if has_reset:
            ok = ok and f.resets_at is not None and f.resets_at > time.time()
        check(f"{text[:44]!r} -> {kind}", ok,
              f"got {got}/{f.window if f else '-'}")

    check("hard failures are the ones worth benching",
          q.classify("weekly limit reached").is_hard
          and not q.classify("429 rate limit").is_hard)
    soon = q.parse_reset("try again in 90 seconds")
    check("relative resets parse", soon is not None and 80 < soon - time.time() < 100)


def test_config_parallel_bound() -> None:
    print("\n[1b] max_parallel is bounded at configuration load")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "leftover.toml"
        for configured in (0, -7):
            path.write_text(f"[agora]\nmax_parallel = {configured}\n")
            cfg = load(path)
            check(f"max_parallel={configured} clamps to one",
                  cfg.max_parallel == 1, str(cfg.max_parallel))


async def test_success_result_refusal_boundary() -> None:
    print("\n[1a] successful short answers are not generic error strings")
    with tempfile.TemporaryDirectory() as tmp:
        spec = AgentSpec(key="claude", label="Claude")
        cfg = Config(agents=[spec], data_dir=tmp)
        router = Router(cfg, object())
        answers = [
            "Retry timeout, HTTP 401, 429, and 500 responses with bounded backoff.",
            "A 429 rate limit response should preserve the original request id.",
            "HTTP 401 Unauthorized means the token must be refreshed first.",
            "The timeout and 500 paths now return a typed result to the caller.",
            "Unauthorized means the caller should refresh its token.",
            "Rate limit exceeded means the backoff window should grow.",
            "Quota exceeded is the example response used in this test.",
            "You've hit your weekly limit is the vendor string we document.",
        ]
        for answer in answers:
            failure = router.observe(spec, Turn(agent=spec, text=answer))
            check(f"normal answer survives: {answer[:34]!r}", failure is None,
                  "" if failure is None else failure.kind)

        refusals = [
            ("You've hit your weekly limit · resets Mon 12:00am", "quota"),
            ("spend limit reached (daily; resets 2027-08-09 00:00 UTC)", "quota"),
            ("API Error: Request rejected (429)", "rate_limit"),
            ("429 rate limit", "rate_limit"),
            ("429 Too Many Requests. Please try again in 30 seconds", "rate_limit"),
            ("Please log in to continue", "auth"),
            ("401 Unauthorized.", "auth"),
        ]
        for body, expected in refusals:
            failure = router.observe(spec, Turn(agent=spec, text=body))
            check(f"CLI refusal still falls back: {expected}",
                  failure is not None and failure.kind == expected,
                  "none" if failure is None else failure.kind)

        class Pool:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def run(self, called, _prompt, _on_event):
                self.calls.append(called.key)
                return Turn(
                    agent=called,
                    text=("Retry timeout, HTTP 401, 429, and 500 responses "
                          "with bounded backoff."),
                )

        fallback = AgentSpec(key="gpt", label="Codex")
        pool = Pool()
        routed = Router(Config(agents=[spec, fallback], data_dir=tmp), pool)
        turn, decision = await routed.run(
            lambda _called: "explain errors",
            primary=spec,
            ordered_chain=[spec, fallback],
            max_attempts=2,
        )
        check("normal status-code answer does not invoke fallback",
              turn.agent is spec and decision.chosen is spec
              and pool.calls == ["claude"],
              f"calls={pool.calls}, chosen={getattr(decision.chosen, 'key', None)}")


def test_codex_probe() -> None:
    print("\n[2] reading Codex's own rate-limit windows")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / ".codex"
        day = home / "sessions" / "2026" / "08" / "21"
        day.mkdir(parents=True)
        # An older file with nothing useful, plus the real reading.
        (day / "rollout-old.jsonl").write_text(json.dumps(
            {"payload": {"type": "token_count", "rate_limits": {
                "primary": None, "secondary": None}}}) + "\n")
        time.sleep(0.02)
        (day / "rollout-new.jsonl").write_text("\n".join([
            json.dumps({"payload": {"type": "turn_context", "model": "gpt-5"}}),
            json.dumps({"payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": 42.5, "window_minutes": 300,
                            "resets_in_seconds": 3600},
                "secondary": {"used_percent": 88.0, "window_minutes": 10080,
                              "resets_in_seconds": 200000}}}}),
        ]) + "\n")

        quota = q.probe_codex(home)
        check("probe found a reading", quota is not None)
        assert quota
        names = {w.name for w in quota.windows}
        check("both windows parsed", names == {"5h", "7d"}, str(names))
        check("headroom is the worst window",
              abs(quota.headroom - 0.12) < 0.01, f"{quota.headroom:.3f}")
        check("marked as reported, not guessed", quota.best_source == q.REPORTED)
        check("reset times are in the future",
              all(w.resets_at and w.resets_at > time.time() for w in quota.windows))
        check("nothing there -> no quota, no crash",
              q.probe_codex(Path(tmp) / "nope") is None)


def test_keychain_write_keeps_the_token_off_argv() -> None:
    print("\n[2b0] a refreshed OAuth token never reaches the process table")
    seen: dict[str, object] = {}

    class _Done:
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["input"] = kwargs.get("input")
        return _Done()

    original = q.subprocess.run
    q.subprocess.run = fake_run
    try:
        ok = q._keychain_set_password("Claude Code-credentials", "me", "s3cret")
    finally:
        q.subprocess.run = original
    check("the write is reported as done", ok)
    check("the secret is passed on stdin",
          seen.get("input") == "s3cret")
    check("the secret is not an argv element",
          "s3cret" not in seen.get("cmd", []), str(seen.get("cmd")))


def test_claude_usage_parse() -> None:
    print("\n[2b] Claude /api/oauth/usage")
    limits = q.parse_claude_usage({
        "limits": [
            {"kind": "session", "percent": 12.0,
             "resets_at": "2099-01-01T00:00:00+00:00"},
            {"kind": "weekly_all", "percent": 40.0,
             "resets_at": "2099-01-08T00:00:00+00:00"},
            {"kind": "weekly_scoped", "percent": 55.0,
             "resets_at": "2099-01-08T00:00:00+00:00",
             "scope": {"model": {"display_name": "Opus"}}},
        ]
    }, plan="max_5x")
    check("limits array parsed", limits is not None)
    assert limits
    names = {w.name: w.used_percent for w in limits.windows}
    check("session is the 5h window", names.get("5h") == 12.0, str(names))
    check("weekly all-models", names.get("weekly") == 40.0, str(names))
    check("scoped opus weekly", names.get("weekly Opus") == 55.0, str(names))
    check("reported, not guessed", limits.best_source == q.REPORTED)
    check("headroom is the worst live window",
          abs(limits.headroom - 0.45) < 0.01, f"{limits.headroom:.3f}")

    flat = q.parse_claude_usage({
        "five_hour": {"utilization": 8.0, "resets_at": "2099-01-01T00:00:00Z"},
        "seven_day": {"utilization": 22.0, "resets_at": "2099-01-08T00:00:00Z"},
        "seven_day_opus": None,
    })
    check("flat keys still parse",
          flat is not None and {w.name for w in flat.windows} == {"5h", "weekly"})
    extra = q.parse_claude_usage({
        "five_hour": {"utilization": 10.0},
        "extra_usage": {"is_enabled": True, "used_credits": 9300, "monthly_limit": 10000},
    })
    check("extra credits stay out of headroom",
          extra is not None and extra.headroom > 0.8
          and extra.windows[0].name == "5h",
          extra.describe() if extra else "none")
    check("extra credits still visible in the note",
          extra is not None and "extra $" in extra.note)
    check("empty payload is no quota", q.parse_claude_usage({}) is None)


def test_grok_billing_parse() -> None:
    print("\n[2c] Grok CLI-proxy billing")
    weekly = q.parse_grok_billing({
        "config": {
            "creditUsagePercent": 59.0,
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": "2026-08-16T15:45:53+00:00",
                "end": "2099-08-23T15:45:53+00:00",
            },
            "billingPeriodEnd": "2099-08-23T15:45:53+00:00",
            "productUsage": [
                {"product": "GrokBuild", "usagePercent": 43},
                {"product": "GrokChat", "usagePercent": 10},
                {"product": "GrokTasks"},
            ],
        }
    }, plan="SuperGrok Heavy")
    check("credits percent parsed", weekly is not None)
    assert weekly
    check("labelled weekly, not monthly",
          weekly.windows[0].name == "weekly", weekly.windows[0].name)
    check("59% used", abs(weekly.windows[0].used_percent - 59.0) < 0.01)
    check("plan lands in the note", "SuperGrok Heavy" in weekly.note)
    check("title is the official weekly pool",
          weekly.title.startswith("official weekly pool")
          and "SuperGrok Heavy" in weekly.title)
    check("product percents drop empty rows",
          weekly.products == [{"name": "Build", "percent": 43.0},
                              {"name": "Chat", "percent": 10.0}])
    check("period start is kept for calendar",
          weekly.windows[0].started_at is not None)

    acp = q.parse_grok_billing({
        "monthlyLimit": {"val": 99900},
        "usage": {"totalUsed": {"val": 24975}},
        "billingCycle": {"billingPeriodEnd": "2099-09-01T00:00:00Z"},
    })
    check("ACP shape still parses", acp is not None)
    assert acp
    check("ACP percent is used/limit",
          abs(acp.windows[0].used_percent - 25.0) < 0.01,
          f"{acp.windows[0].used_percent:.1f}")

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / ".grok"
        home.mkdir()
        (home / "auth.json").write_text(json.dumps({
            "https://auth.x.ai::client": {
                "key": "tok",
                "auth_mode": "oidc",
                "expires_at": "2099-01-01T00:00:00Z",
                "user_id": "u1",
            },
            "xai::api_key": {"key": "xai-nope", "auth_mode": "api_key"},
        }))
        picked = q._grok_auth_entry(home)
        check("oidc session beats api key",
              picked is not None and picked.get("key") == "tok")
        (home / "auth.json").write_text(json.dumps({
            "https://auth.x.ai::client": {
                "key": "old",
                "auth_mode": "oidc",
                "expires_at": "2020-01-01T00:00:00Z",
            }
        }))
        check("expired grok token is ignored", q._grok_auth_entry(home) is None)


def test_sub2api_codex_probe() -> None:
    print("\n[2e] Sub2API admin Codex usage")
    usage = {
        "code": 0,
        "data": {
            "five_hour": {
                "utilization": 0,
                "resets_at": "2026-08-23T08:28:48+08:00",
                "remaining_seconds": 0,
                "window_stats": {
                    "requests": 342,
                    "tokens": 26775073,
                    "cost": 44.975962,
                },
            },
            "seven_day": {
                "utilization": 38,
                "resets_at": "2026-08-27T12:28:49+08:00",
                "remaining_seconds": 359970,
                "window_stats": {
                    "requests": 6499,
                    "tokens": 663999548,
                    "cost": 1059.25545824,
                },
            },
        },
    }
    quota = q.parse_sub2api_usage(usage, account_name="acme-team")
    check("usage payload parsed", quota is not None)
    assert quota
    names = {w.name: w for w in quota.windows}
    check("inactive 0/0 5h shell is discarded",
          set(names) == {"weekly"}, str(set(names)))
    check("weekly 38% with remaining_seconds",
          abs(names["weekly"].used_percent - 38) < 0.01
          and names["weekly"].resets_at is not None
          and 359000 < names["weekly"].resets_at - time.time() < 361000)
    check("valid weekly stats survive the shell filter",
          names["weekly"].requests == 6499
          and names["weekly"].cost_usd == 1059.25545824)
    check("reported", quota.best_source == q.REPORTED)
    check("account lands in the note", "acme-team" in quota.note)

    stale_extra = {
        "codex_5h_used_percent": 0,
        "codex_5h_window_minutes": 0,
    }
    live_reset = time.time() + 7200
    live = q.parse_sub2api_usage({
        "data": {
            "five_hour": {
                "utilization": 17,
                "remaining_seconds": 7200,
                "resets_at": live_reset,
            },
        },
    }, account_name="acme-team", account_extra=stale_extra)
    check("live usage beats stale disabled account metadata",
          live is not None and len(live.windows) == 1
          and live.windows[0].name == "5h"
          and live.windows[0].used_percent == 17
          and live.windows[0].resets_at is not None
          and live.windows[0].resets_at > time.time() + 7100,
          live.describe() if live else "none")

    zero = q.parse_sub2api_usage({
        "data": {
            "five_hour": {
                "used_percent": 0,
                "remaining_seconds": 18000,
                "resets_at": time.time() + 18000,
            },
        },
    })
    check("an explicit numeric zero remains a valid live percentage",
          zero is not None and len(zero.windows) == 1
          and zero.windows[0].used_percent == 0,
          zero.describe() if zero else "none")

    shell = q.parse_sub2api_usage({
        "data": {
            "five_hour": {
                "utilization": 0,
                "remaining_seconds": 0,
                "resets_at": time.time() - 60,
            },
        },
    }, account_name="acme-team", account_extra=stale_extra)
    check("disabled metadata filters only a matching inactive live shell",
          shell is None, shell.describe() if shell else "none")

    extra = q.parse_sub2api_account({
        "id": 1, "name": "acme-team", "platform": "openai", "type": "oauth",
        "extra": {
            "codex_5h_used_percent": 12,
            "codex_5h_reset_at": "2099-01-01T00:00:00+00:00",
            "codex_5h_window_minutes": 300,
            "codex_7d_used_percent": 40,
            "codex_7d_reset_at": "2099-01-08T00:00:00+00:00",
            "codex_7d_window_minutes": 10080,
        },
    })
    check("account extra still parses",
          extra is not None and {w.name: w.used_percent for w in extra.windows}
          == {"5h": 12.0, "weekly": 40.0})

    shell_extra = q.parse_sub2api_account({
        "id": 1, "name": "acme-team", "platform": "openai", "type": "oauth",
        "extra": {
            "codex_5h_used_percent": 0,
            "codex_5h_reset_at": "2099-01-01T00:00:00+00:00",
            "codex_5h_window_minutes": 0,
            "codex_7d_used_percent": 38,
            "codex_7d_reset_at": "2099-01-08T00:00:00+00:00",
            "codex_7d_window_minutes": 10080,
        },
    })
    check("account extra filters a disabled 5h window",
          shell_extra is not None
          and {w.name: w.used_percent for w in shell_extra.windows}
          == {"weekly": 38.0})

    items = [
        {"id": 5, "name": "Xinrun SuperGrokHeavy", "platform": "grok",
         "type": "oauth", "status": "active", "extra": {}},
        {"id": 4, "name": "aux_account", "platform": "openai",
         "type": "apikey", "status": "active", "extra": {"quota_weekly_used": 98}},
        {"id": 1, "name": "acme-team", "platform": "openai",
         "type": "oauth", "status": "active",
         "extra": {
             "codex_5h_used_percent": 0,
             "codex_5h_window_minutes": 0,
             "codex_7d_used_percent": 38,
             "codex_7d_window_minutes": 10080,
         }},
        {"id": 9, "name": "Plus 20x", "platform": "openai",
         "type": "oauth", "status": "active",
         "extra": {"codex_5h_used_percent": 10}},
    ]
    check("pin 20x matches name substring",
          (q.pick_sub2api_account(items, "20x") or {}).get("id") == 9)
    check("pin id", (q.pick_sub2api_account(items, "1") or {}).get("name") == "acme-team")
    check("auto prefers openai oauth with Codex extra over apikey",
          (q.pick_sub2api_account(items[:3], "") or {}).get("id") == 1)
    check("unknown pin is None", q.pick_sub2api_account(items, "missing") is None)

    class Fake:
        configured = True
        base_url = "https://api.example.com:8443/"
        admin_key = "admin-test"
        gpt_account = "acme-team"

    def fake_http(method, url, headers=None, body=None, timeout=8.0):
        if "/accounts?" in url:
            return 200, {"code": 0, "data": {
                "items": items[:3], "pages": 1, "total": 3}}
        if url.endswith("/usage?source=active"):
            return 200, usage
        return 0, None

    orig = q._http_json
    q._http_json = fake_http  # type: ignore[assignment]
    try:
        found = q.probe_sub2api(Fake())
        check("probe hits usage and ignores the disabled 5h shell",
              found is not None
              and {w.name: w.used_percent for w in found.windows}
              == {"weekly": 38.0}
              and found.best_source == q.REPORTED,
              found.describe() if found else "none")
        via_codex = q.probe_codex(sub2api=Fake())
        check("codex probe prefers sub2api when configured",
              via_codex is not None and "sub2api" in (via_codex.note or ""),
              via_codex.note if via_codex else "none")
    finally:
        q._http_json = orig


def test_cursor_usage_parse() -> None:
    print("\n[2d] Cursor GetCurrentPeriodUsage")
    quota = q.parse_cursor_usage({
        "billingCycleStart": 1786896184000,
        "billingCycleEnd": 4102444800000,
        "planUsage": {
            "totalSpend": 10628,
            "remaining": 29372,
            "limit": 40000,
            "autoPercentUsed": 2.656,
            "apiPercentUsed": 10.632,
        },
        "displayMessage": "You've used 27% of your included usage",
    }, tier="Ultra")
    check("dashboard parsed", quota is not None)
    assert quota
    monthly = next(w for w in quota.windows if w.name == "monthly")
    check("included spend is the monthly window",
          abs(monthly.used_percent - 26.57) < 0.05, f"{monthly.used_percent:.2f}")
    check("reset is in the future",
          monthly.resets_at is not None and monthly.resets_at > time.time())
    check("reported", quota.best_source == q.REPORTED)
    check("plan in the note", "Ultra" in quota.note)
    check("included spend is dollars from cents",
          abs(quota.extras.get("included_used_usd") - 106.28) < 0.01
          and quota.extras.get("included_limit_usd") == 400.0)
    check("cycle start kept", quota.windows[0].started_at is not None)
    check("no numbers -> None", q.parse_cursor_usage({"planUsage": {}}) is None)


def test_quota_rhythm() -> None:
    print("\n[2f] quota rhythm bars and same-window deltas")
    from datetime import datetime
    from zoneinfo import ZoneInfo
    london = ZoneInfo("Europe/London")
    now = datetime(2026, 8, 22, 0, 18, tzinfo=london).timestamp()
    start = datetime(2026, 8, 16, 16, 45, 53, tzinfo=london).timestamp()
    end = datetime(2026, 8, 23, 16, 45, 53, tzinfo=london).timestamp()
    check("bar 75.9% is 12/16", rh.bar(75.9) == "████████████░░░░")
    check("bar 60% is 10/16", rh.bar(60) == "██████████░░░░░░")
    grok = q.Window("weekly", 60.0, resets_at=end, started_at=start,
                    source=q.REPORTED)
    cal = rh.calendar_pct(grok, now)
    check("grok calendar ~75.9%", cal is not None and abs(cal - 75.9) < 0.2,
          f"{cal}")
    prev = q.Window("weekly", 59.0, resets_at=end, started_at=start,
                    source=q.REPORTED)
    tags = rh.pace_tags(grok, prev, now=now, prev_now=now - 3600)
    check("lag + increase + narrow",
          rh.BEHIND in tags and any(t.startswith("↑") for t in tags)
          and "narrowing" in tags,
          str(tags))
    fresh = q.Window("5h", 0.0, resets_at=now + 5 * 3600, started_at=now,
                     source=q.REPORTED)
    check("just-reset 5h", rh.just_reset(fresh, now))
    new = q.Window("weekly", 1.0, resets_at=end + 7 * 86400,
                   started_at=end, source=q.REPORTED)
    check("new window tag",
          "new window from 0" in rh.pace_tags(new, grok, now=now, prev_now=now))
    spec = AgentSpec(key="grok", label="Grok", emoji="X")
    block = rh.render_grok(
        q.Quota("grok", [grok], title="official weekly pool · SuperGrok Heavy",
                products=[{"name": "Build", "percent": 43}]),
        q.Quota("grok", [prev], checked_at=now - 3600),
        now, london)
    check("grok block has both bars",
          "calendar ████████████░░░░" in block
          and "used     ██████████░░░░░░" in block)
    check("grok footer has products", "Build 43%" in block)
    cursor = q.parse_cursor_usage({
        "billingCycleStart": (now - 5.3 * 86400) * 1000,
        "billingCycleEnd": (now + 25 * 86400) * 1000,
        "planUsage": {
            "totalSpend": 15116, "remaining": 24884, "limit": 40000,
            "autoPercentUsed": 4.9, "apiPercentUsed": 11.0,
        },
    }, tier="ultra")
    assert cursor
    text = rh.render_cursor(cursor, None, now, london)
    check("cursor header is included dollars",
          "included $151.16 / $400" in text and "left $248.84" in text)
    check("cursor splits Models and Other api%",
          "Models" in text and "Other api%" in text)
    gpt_week = q.Window("weekly", 16.0, resets_at=end + 4 * 86400,
                        started_at=end - 3 * 86400, source=q.REPORTED,
                        requests=2800, cost_usd=399.38)
    gpt_5h = q.Window("5h", 0.0, resets_at=now + 5 * 3600, started_at=now,
                      source=q.REPORTED)
    gpt_block = rh.render_windows(
        AgentSpec(key="gpt", label="Codex", emoji="G"),
        q.Quota("gpt", [gpt_5h, gpt_week], title="you@example.com"),
        None, now, london)
    check("codex 5h is a footnote, not bars",
          "5h just reset" in gpt_block and gpt_block.count("calendar █") == 1)
    check("codex keeps the 7d req/$ line",
          "2.8K req" in gpt_block and "$399.38" in gpt_block)
    page = rh.render(
        [(spec, q.Quota("grok", [grok],
                        title="official weekly pool · SuperGrok Heavy"), None)],
        now=now, strategy="lag_waste", order=["gpt", "grok"],
        tz_name="Europe/London")
    check("page header is London stamp",
          "usage rhythm  ·  22 Aug 2026 00:18 London" in page)
    check("legend is present", "widening/narrowing compares one window" in page)
    local = rh.render(
        [(spec, q.Quota("grok", [grok], title="official weekly pool"), None)],
        now=now, strategy="lag_waste")
    stamp = rh._when(now, rh._tz(""), with_year=True)
    check("no tz configured means this machine's clock, not London",
          f"usage rhythm  ·  {stamp} " in local, local.splitlines()[0])
    check("the view is English so a non-Chinese reader can use /quota",
          not any("\u4e00" <= ch <= "\u9fff" for ch in local), local)


def test_ledger() -> None:
    print("\n[3] local ledger for agents that report nothing")
    with tempfile.TemporaryDirectory() as tmp:
        led = q.Ledger(Path(tmp) / "ledger.json")
        for _ in range(6):
            led.record("grok", 2.0, ok=True)
        led.record("grok", 1.0, ok=False)
        check("successful turns counted", led.count("grok", 3600) == 6)
        check("failures counted separately",
              led.count("grok", 3600, successful_only=False) == 7)
        windows = led.budget_windows("grok", per_5h=12, per_week=100)
        check("budget becomes a percentage",
              abs(windows[0].used_percent - 50.0) < 0.01,
              f"{windows[0].used_percent:.0f}%")
        check("labelled as an estimate", windows[0].source == q.ESTIMATED)
        check("survives a reload", q.Ledger(Path(tmp) / "ledger.json").count("grok", 3600) == 6)


async def test_quota_fallback() -> None:
    print("\n[4] an agent that is out of quota is replaced mid-turn")
    with tempfile.TemporaryDirectory() as tmp:
        cfg, router = make([agent("claude", "quota_weekly", fallback=["gpt"]),
                            agent("gpt"), agent("cursor")], tmp)
        orch = Orchestrator(cfg, router.pool, router)

        turns = await orch.execute(orch.parse("@claude plan the migration",
                                              in_group=True), None)
        check("the chat still got an answer", turns[0].ok, turns[0].short(40))
        check("answered by the declared fallback", turns[0].agent.key == "gpt",
              turns[0].agent.key)
        check("substitution is reported, not hidden",
              "claude -> gpt" in (orch.last_decision.describe() if orch.last_decision else ""),
              orch.last_decision.describe() if orch.last_decision else "")

        health = router.health["claude"]
        check("benched, not just retried", health.state is State.COOLING)
        check("benched until the reset it named",
              health.until > time.time() + 3600, health.describe())
        check("quota view explains why",
              "100% used" in (await router.quota_for(cfg.agents[0])).describe())

        ranked = await router.rank(cfg.enabled_agents())
        check("dropped to the back of the queue", ranked[-1].key == "claude",
              " ".join(a.key for a in ranked))

        turns = await orch.execute(orch.parse("@claude and again", in_group=True), None)
        check("second ask skips it without even trying",
              turns[0].agent.key != "claude" and turns[0].ok)
        await router.pool.shutdown()


async def test_quota_probe_preserves_concurrent_observed_limit() -> None:
    print("\n[4b] a quota probe cannot erase a concurrent refusal")

    class RacingRouter(Router):
        def __init__(self, config: Config, pool) -> None:
            super().__init__(config, pool)
            self.probe_started = asyncio.Event()
            self.release_probe = asyncio.Event()

        async def _probe_quota(self, spec: AgentSpec, deadline: float):
            self.probe_started.set()
            await self.release_probe.wait()
            return q.Quota(agent=spec.key)

    spec = AgentSpec(key="shared", label="Shared")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(agents=[spec], data_dir=tmp)
        router = RacingRouter(cfg, object())
        probe = asyncio.create_task(router.quota_for(spec, force=True))
        await router.probe_started.wait()
        router.observe(spec, Turn(
            agent=spec,
            error="You've hit your weekly limit; resets tomorrow at 12:00am",
        ))
        router.release_probe.set()
        result = await probe

    observed = [
        window for window in result.windows
        if window.source == q.OBSERVED and window.used_percent == 100.0
    ]
    check("the in-flight probe result retains the observed limit",
          bool(observed), result.describe())
    check("shared router health retains the same observed limit",
          router.h(spec).quota is result
          and any(window.source == q.OBSERVED
                  for window in router.h(spec).quota.windows),
          router.h(spec).quota.describe())


async def test_fresh_quota_replaces_stale_observed_without_reset() -> None:
    print("\n[4c] fresh reported quota replaces a stale undated refusal")

    class FreshRouter(Router):
        async def _probe_quota(self, spec: AgentSpec, deadline: float):
            return q.Quota(
                agent=spec.key,
                windows=[q.Window(
                    name="weekly",
                    used_percent=10.0,
                    source=q.REPORTED,
                )],
            )

    spec = AgentSpec(key="fresh-quota", label="Fresh quota")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(agents=[spec], data_dir=tmp)
        router = FreshRouter(cfg, object())
        router.h(spec).quota = q.Quota(
            agent=spec.key,
            windows=[q.Window(
                name="limit",
                used_percent=100.0,
                source=q.OBSERVED,
                detail="undated historical refusal",
            )],
        )
        router.h(spec).quota_checked = 0.0
        result = await router.quota_for(spec, force=True)

    check("authoritative reported data clears the stale observed limit",
          len(result.windows) == 1
          and result.windows[0].source == q.REPORTED
          and abs(result.headroom - 0.9) < 0.001,
          f"{result.describe()}, headroom={result.headroom}")


async def test_concurrent_probes_do_not_revive_stale_observed_limit() -> None:
    print("\n[4d] concurrent probes cannot revive a stale refusal")

    class DualProbeRouter(Router):
        def __init__(self, config: Config, pool) -> None:
            super().__init__(config, pool)
            self.calls = 0
            self.both_started = asyncio.Event()
            self.release_silent = asyncio.Event()
            self.release_reported = asyncio.Event()

        async def _probe_quota(self, spec: AgentSpec, deadline: float):
            self.calls += 1
            call = self.calls
            if self.calls == 2:
                self.both_started.set()
            if call == 1:
                await self.release_silent.wait()
                return None
            await self.release_reported.wait()
            return q.Quota(
                agent=spec.key,
                windows=[q.Window(
                    name="weekly",
                    used_percent=10.0,
                    source=q.REPORTED,
                )],
            )

    spec = AgentSpec(key="dual-probe", label="Dual probe")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(agents=[spec], data_dir=tmp)
        router = DualProbeRouter(cfg, object())
        health = router.h(spec)
        health.quota = q.Quota(
            agent=spec.key,
            windows=[q.Window(
                name="limit",
                used_percent=100.0,
                source=q.OBSERVED,
                detail="undated historical refusal",
            )],
        )
        health.quota_observation_epoch = 1
        silent = asyncio.create_task(router.quota_for(spec, force=True))
        reported = asyncio.create_task(router.quota_for(spec, force=True))
        await router.both_started.wait()
        router.release_silent.set()
        silent_result = await silent
        router.release_reported.set()
        reported_result = await reported

    check("a silent concurrent probe may retain the cached refusal",
          any(window.source == q.OBSERVED
              for window in silent_result.windows),
          silent_result.describe())
    check("a later reported probe does not misclassify that copy as new",
          len(reported_result.windows) == 1
          and reported_result.windows[0].source == q.REPORTED
          and abs(reported_result.headroom - 0.9) < 0.001
          and router.h(spec).quota is reported_result,
          f"{reported_result.describe()}, headroom={reported_result.headroom}")


async def test_circuit_breaker() -> None:
    print("\n[5] a flapping agent trips a breaker with backoff")
    with tempfile.TemporaryDirectory() as tmp:
        cfg, router = make([agent("claude", "crash"), agent("gpt")], tmp,
                           trip_after=2, base_cooldown=60, max_cooldown=600)
        spec = cfg.agents[0]
        orch = Orchestrator(cfg, router.pool, router)

        await orch.execute(orch.parse("@claude one", in_group=True), None)
        check("one failure is not enough to bench it",
              router.health["claude"].state is State.OK,
              router.health["claude"].state.value)
        router.health["claude"].state = State.OK       # allow a second attempt
        turn, _ = await router.run(lambda s: "two", primary=spec, max_attempts=1)
        check("second consecutive failure trips it",
              router.health["claude"].state is State.TRIPPED)
        first_until = router.health["claude"].until
        check("cooldown starts at the base value",
              50 < first_until - time.time() < 70,
              f"{first_until - time.time():.0f}s")

        router.health["claude"].until = time.time() - 1
        check("expired breaker goes half-open, not straight to ok",
              router.health["claude"].usable
              and router.health["claude"].state is State.HALF_OPEN)

        turn, _ = await router.run(lambda s: "three", primary=spec, max_attempts=1)
        check("failing the probe backs off further",
              router.health["claude"].until - time.time() > first_until - time.time(),
              f"{router.health['claude'].until - time.time():.0f}s")
        await router.pool.shutdown()


async def test_recovery_and_auto() -> None:
    print("\n[6] recovery, @any routing and headroom ordering")
    with tempfile.TemporaryDirectory() as tmp:
        cfg, router = make([agent("claude"), agent("gpt"), agent("cursor")], tmp)
        spec = cfg.agents[0]
        router.health["claude"].state = State.TRIPPED
        router.health["claude"].consecutive = 3
        router.health["claude"].until = time.time() - 1

        turn, _ = await router.run(lambda s: "probe", primary=spec, max_attempts=1)
        check("a good turn clears the breaker",
              turn.ok and router.health["claude"].state is State.OK)
        check("failure counter reset", router.health["claude"].consecutive == 0)

        orch = Orchestrator(cfg, router.pool, router)
        plan = orch.parse("@any who has room", in_group=True)
        check("@any parses as auto", plan is not None and plan.auto and not plan.agents)
        turns = await orch.execute(plan, None)
        check("auto still produces one answer", len(turns) == 1 and turns[0].ok,
              turns[0].agent.key)

        # Burn gpt's declared budget; headroom should demote it.
        cfg.agents[1].budget_5h_turns = 4
        for _ in range(4):
            router.ledger.record("gpt", 1.0, ok=True)
        router.health["gpt"].quota_checked = 0.0
        ranked = await router.rank(cfg.enabled_agents())
        check("exhausted budget sinks in the ranking",
              ranked[-1].key == "gpt", " ".join(a.key for a in ranked))

        report = await router.report()
        check("report names every agent",
              all(k in report for k in ("Claude", "Gpt", "Cursor")), report)
        check("rhythm header",
              "usage rhythm" in report and "calendar" in report.split("\n")[1])
        check("report shows the strategy", "strategy: headroom" in report)
        await router.pool.shutdown()


async def test_group_substitution() -> None:
    print("\n[7] group modes substitute without duplicating a speaker")
    with tempfile.TemporaryDirectory() as tmp:
        cfg, router = make([agent("claude", "quota_session"), agent("gpt"),
                            agent("cursor")], tmp)
        orch = Orchestrator(cfg, router.pool, router)
        turns = await orch.execute(orch.parse("/rt should we ship", in_group=True), None)
        speakers = [t.agent.key for t in turns if t.ok]
        check("nobody speaks twice", len(speakers) == len(set(speakers)),
              " ".join(speakers))
        check("the out-of-quota agent is not among them",
              "claude" not in speakers, " ".join(speakers))
        check("the round still had real answers", len(speakers) >= 2)

        plan = orch.parse("@gpt @cursor compare", in_group=True)
        turns = await orch.execute(plan, None)
        check("parallel slots keep their own agents",
              sorted(t.agent.key for t in turns) == ["cursor", "gpt"],
              " ".join(t.agent.key for t in turns))
        await router.pool.shutdown()


async def test_parallel_fallback_shares_one_ranking() -> None:
    print("\n[7b] parallel fallback shares one quota-aware ranking")

    class FailingPrimaryPool:
        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            await asyncio.sleep(0.005)
            if spec.key.startswith("primary"):
                return Turn(agent=spec, error="connection reset by peer")
            return Turn(agent=spec, text=f"{spec.key} recovered")

    class CountingRouter(Router):
        def __init__(self, config: Config, pool) -> None:
            super().__init__(config, pool)
            self.rank_calls = 0

        async def rank(self, specs: list[AgentSpec]) -> list[AgentSpec]:
            self.rank_calls += 1
            await asyncio.sleep(0.02)
            return await super().rank(specs)

    installed = [sys.executable]
    agents = [
        AgentSpec(key="primary-a", label="Primary A",
                  interactive_command=installed),
        AgentSpec(key="primary-b", label="Primary B",
                  interactive_command=installed),
        AgentSpec(key="spare-a", label="Spare A",
                  interactive_command=installed),
        AgentSpec(key="spare-b", label="Spare B",
                  interactive_command=installed),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        pool = FailingPrimaryPool()
        cfg = Config(
            agents=agents,
            data_dir=tmp,
            routing=Routing(strategy="order", order=[a.key for a in agents]),
        )
        router = CountingRouter(cfg, pool)
        turns = await Orchestrator(cfg, pool, router).execute(
            Plan("broadcast", "recover", agents[:2], {}), None)

    check("simultaneous primary failures trigger one shared rank call",
          router.rank_calls == 1, str(router.rank_calls))
    check("shared ranking still assigns distinct healthy spares",
          {turn.agent.key for turn in turns} == {"spare-a", "spare-b"}
          and all(turn.ok for turn in turns),
          repr([(turn.agent.key, turn.error) for turn in turns]))


async def test_group_role_reservations() -> None:
    print("\n[8] debate and relay keep distinct role owners")

    class RolePool:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def peek(self, spec: AgentSpec):
            return None

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            if "arguing FOR" in prompt:
                role = "FOR"
            elif "arguing AGAINST" in prompt:
                role = "AGAINST"
            elif ("You are the neutral judge" in prompt
                  or "You are the judge" in prompt):
                role = "JUDGE"
            elif "concrete implementation plan" in prompt:
                role = "PLAN"
            elif "Carry out the plan" in prompt:
                role = "IMPLEMENT"
            else:
                role = "REVIEW"
            self.calls.append((spec.key, role))
            if spec.key == "claude":
                return Turn(agent=spec,
                            text="You've hit your weekly limit · resets Mon 12:00am")
            return Turn(agent=spec, text=f"{spec.key} handled {role}")

    async def run_mode(mode: str, spare: bool) -> list[tuple[str, str]]:
        with tempfile.TemporaryDirectory() as tmp:
            agents = [
                AgentSpec(key="claude", label="Claude", fallback=["gpt", "cursor"],
                          interactive_command=[sys.executable]),
                AgentSpec(key="gpt", label="Gpt",
                          interactive_command=[sys.executable]),
                AgentSpec(key="grok", label="Grok",
                          interactive_command=[sys.executable]),
            ]
            if spare:
                agents.append(AgentSpec(
                    key="cursor", label="Cursor",
                    interactive_command=[sys.executable]))
            pool = RolePool()
            cfg = Config(
                agents=agents,
                data_dir=tmp,
                routing=Routing(strategy="order",
                                order=[agent.key for agent in agents]),
            )
            orch = Orchestrator(cfg, pool, Router(cfg, pool))
            plan = Plan(mode, "ship safely", agents[:3],
                        {"rounds": "1"} if mode == "debate" else {})
            await orch.execute(plan, None)
            return pool.calls

    debate = await run_mode("debate", spare=False)
    check("a debater cannot fall back onto the opposing side",
          ("gpt", "FOR") not in debate and ("gpt", "AGAINST") in debate,
          str(debate))
    check("the neutral judge prompt keeps its debate role",
          ("grok", "JUDGE") in debate, str(debate))
    relay = await run_mode("relay", spare=False)
    check("relay stages never reuse another assigned role",
          ("gpt", "PLAN") not in relay and ("gpt", "IMPLEMENT") in relay,
          str(relay))
    debate_spare = await run_mode("debate", spare=True)
    check("debate fallback uses an unassigned spare",
          ("cursor", "FOR") in debate_spare
          and ("gpt", "AGAINST") in debate_spare,
          str(debate_spare))
    relay_spare = await run_mode("relay", spare=True)
    check("relay fallback uses an unassigned spare",
          relay_spare == [
              ("claude", "PLAN"), ("cursor", "PLAN"),
              ("gpt", "IMPLEMENT"), ("grok", "REVIEW")],
          str(relay_spare))


async def test_debate_turn_timeout_is_bounded() -> None:
    print("\n[9] debate turn timeout cancels a stuck advocate")

    class TimeoutPool:
        def __init__(self) -> None:
            self.cancelled = asyncio.Event()

        def peek(self, spec: AgentSpec):
            return None

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            if spec.key == "pro":
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.set()
            await asyncio.sleep(0.005)
            text = "neutral verdict" if "neutral judge" in prompt else (
                f"{spec.key} argument")
            return Turn(agent=spec, text=text, seconds=0.005)

    agents = [
        AgentSpec(key="pro", label="Pro"),
        AgentSpec(key="con", label="Con"),
        AgentSpec(key="judge", label="Judge"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        pool = TimeoutPool()
        cfg = Config(
            agents=agents,
            data_dir=tmp,
            debate_rounds=1,
            debate_turn_timeout=0.03,
            routing=Routing(strategy="order", order=[a.key for a in agents]),
        )
        orch = Orchestrator(cfg, pool, Router(cfg, pool))
        started = asyncio.get_running_loop().time()
        turns = await orch.execute(Plan("debate", "ship", agents, {}), None)
        elapsed = asyncio.get_running_loop().time() - started

    check("a stuck advocate is cancelled at the debate deadline",
          pool.cancelled.is_set() and elapsed < 0.25,
          f"cancelled={pool.cancelled.is_set()}, elapsed={elapsed:.3f}s")
    check("the timed-out slot is reported without blocking the verdict",
          len(turns) == 3
          and turns[0].error == "debate turn timed out after 0.03s"
          and turns[0].seconds == 0.03
          and turns[1].ok and turns[2].ok,
          repr([(turn.error, turn.seconds) for turn in turns]))


async def test_acp_idle_timeout_tracks_all_updates() -> None:
    print("\n[9a] ACP idle timeout resets on thought and tool updates")
    from leftover.agents import acp_runner as acp_mod

    class Result:
        stop_reason = "end_turn"

    class Connection:
        async def prompt(self, session_id, prompt):
            for event in (
                    acp_mod.Event("thought", "considering"),
                    acp_mod.Event("tool", "read_file"),
                    acp_mod.Event("thought", "checking"),
                    acp_mod.Event("text", "finished")):
                await asyncio.sleep(0.02)
                await runner._queue.put(event)
            return Result()

    runner = acp_mod.AcpRunner(AgentSpec(
        key="active", label="Active", acp_command=["unused"],
        timeout=1, acp_idle_timeout=0.05))
    runner._conn = Connection()
    runner._session_id = "session"
    started = asyncio.get_running_loop().time()
    turn = await runner.run("keep working")
    elapsed = asyncio.get_running_loop().time() - started

    check("thought/tool activity keeps a longer turn alive",
          turn.ok and turn.text == "finished" and turn.tools == ["read_file"]
          and elapsed > runner.spec.acp_idle_timeout,
          f"elapsed={elapsed:.3f}s, error={turn.error!r}, tools={turn.tools}")


async def test_acp_idle_timeout_cleans_up_silence() -> None:
    print("\n[9b] ACP idle timeout cancels and cleans up a silent prompt")
    from leftover.agents import acp_runner as acp_mod

    state = {"cancelled": 0, "prompt_finished": False}

    class Connection:
        async def prompt(self, session_id, prompt):
            try:
                await asyncio.Event().wait()
            finally:
                state["prompt_finished"] = True

        async def cancel(self, session_id):
            state["cancelled"] += 1

    runner = acp_mod.AcpRunner(AgentSpec(
        key="silent", label="Silent", acp_command=["unused"],
        timeout=1, acp_idle_timeout=0.02))
    runner._conn = Connection()
    runner._session_id = "session"
    original_grace = acp_mod._CANCEL_GRACE_TIMEOUT
    acp_mod._CANCEL_GRACE_TIMEOUT = 0.01
    started = asyncio.get_running_loop().time()
    try:
        turn = await asyncio.wait_for(runner.run("silent"), timeout=0.3)
    finally:
        acp_mod._CANCEL_GRACE_TIMEOUT = original_grace
    elapsed = asyncio.get_running_loop().time() - started

    check("pure ACP silence reports the configured idle boundary",
          turn.error == "ACP idle timed out after 0.02s without an update"
          and turn.meta.get("timeout_kind") == "idle",
          f"elapsed={elapsed:.3f}s, turn={turn}")
    check("idle cleanup cancels the prompt and invalidates the session",
          state == {"cancelled": 1, "prompt_finished": True}
          and not runner.live_session() and elapsed < 0.2,
          f"elapsed={elapsed:.3f}s, state={state}")


async def test_acp_internal_activity_does_not_extend_idle_deadline() -> None:
    print("\n[9c] internal ACP activity cannot hide a user-visible stall")
    from leftover.agents import acp_runner as acp_mod

    state = {"cancel_calls": 0, "prompt_finished": False}

    class Connection:
        async def prompt(self, session_id, prompt):
            try:
                while True:
                    await runner._queue.put(acp_mod._ACTIVITY)
                    await asyncio.sleep(0.005)
            finally:
                state["prompt_finished"] = True

        async def cancel(self, session_id):
            state["cancel_calls"] += 1

    runner = acp_mod.AcpRunner(AgentSpec(
        key="busy", label="Busy", acp_command=["unused"],
        timeout=0.2, acp_idle_timeout=0.03))
    runner._conn = Connection()
    runner._session_id = "session"
    original_grace = acp_mod._CANCEL_GRACE_TIMEOUT
    acp_mod._CANCEL_GRACE_TIMEOUT = 0.01
    started = asyncio.get_running_loop().time()
    try:
        turn = await asyncio.wait_for(runner.run("stay busy"), timeout=0.3)
    finally:
        acp_mod._CANCEL_GRACE_TIMEOUT = original_grace
    elapsed = asyncio.get_running_loop().time() - started

    check("internal activity cannot reset the visible-progress deadline",
          turn.error == "ACP idle timed out after 0.03s without an update"
          and turn.meta.get("timeout_kind") == "idle"
          and 0.02 <= elapsed < 0.15,
          f"elapsed={elapsed:.3f}s, turn={turn}")
    check("idle timeout cancels and retires the internally busy prompt",
          state == {"cancel_calls": 1, "prompt_finished": True}
          and not runner.live_session(), str(state))


async def test_agent_pool_start_timeout_is_a_hard_boundary() -> None:
    print("\n[9d] runner startup timeout does not await stubborn cleanup")
    from leftover import agents as agents_mod

    release = asyncio.Event()
    start_finished = asyncio.Event()
    close_finished = asyncio.Event()
    state = {"start_cancelled": 0, "close_cancelled": 0}

    class StubbornRunner(agents_mod.BaseRunner):
        async def start(self, workdir: str) -> None:
            await super().start(workdir)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["start_cancelled"] += 1
                await release.wait()
                start_finished.set()
                raise RuntimeError("late startup cleanup failure")

        async def close(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["close_cancelled"] += 1
                await release.wait()
                close_finished.set()
                raise RuntimeError("late close cleanup failure")

    original_build = agents_mod.build_runner
    original_start_timeout = agents_mod.START_TIMEOUT
    original_control_timeout = agents_mod._RUNNER_CONTROL_TIMEOUT
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    leaked: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: leaked.append(context))
    agents_mod.build_runner = lambda spec: StubbornRunner(spec)
    agents_mod.START_TIMEOUT = 0.01
    agents_mod._RUNNER_CONTROL_TIMEOUT = 0.01
    spec = AgentSpec(
        key="stubborn-start", label="Stubborn start", transport="acp",
        acp_command=["unused"], exec_command=[sys.executable])
    pool = AgentPool(Config(agents=[spec], default_workdir=str(ROOT)))
    started = loop.time()
    try:
        prepared = await pool.prepare(spec)
        elapsed = loop.time() - started
        await asyncio.sleep(0)
        check("startup plus failed-runner close obey wall-clock bounds",
              elapsed < 0.15 and state == {
                  "start_cancelled": 1, "close_cancelled": 1,
              }, f"elapsed={elapsed:.3f}s, state={state}")
        check("a bounded startup failure installs the exec fallback",
              isinstance(prepared, agents_mod.ExecRunner)
              and pool.peek(spec) is prepared)
    finally:
        release.set()
        await asyncio.wait_for(
            asyncio.gather(start_finished.wait(), close_finished.wait()),
            timeout=0.2)
        await asyncio.sleep(0)
        agents_mod.build_runner = original_build
        agents_mod.START_TIMEOUT = original_start_timeout
        agents_mod._RUNNER_CONTROL_TIMEOUT = original_control_timeout
        await pool.shutdown()
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)
    check("late startup and close failures are retrieved", not leaked,
          repr([context.get("message") for context in leaked]))


async def test_acp_close_is_a_hard_boundary() -> None:
    print("\n[9d] ACP close bounds transport and process cleanup")
    from leftover.agents import acp_runner as acp_mod

    release = asyncio.Event()
    all_finished = asyncio.Event()
    state = {"close_cancelled": 0, "terminate": 0, "kill": 0,
             "wait_cancelled": 0, "finished": 0}

    def finished() -> None:
        state["finished"] += 1
        if state["finished"] == 2:
            all_finished.set()

    class StubbornStack:
        async def aclose(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["close_cancelled"] += 1
                await release.wait()
                finished()
                raise RuntimeError("late stack close failure")

    class StubbornProcess:
        returncode = None

        def terminate(self):
            state["terminate"] += 1

        def kill(self):
            state["kill"] += 1

        async def wait(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["wait_cancelled"] += 1
                await release.wait()
                self.returncode = -9
                finished()
                return self.returncode

    original_close = acp_mod._CLOSE_TIMEOUT
    original_exit = acp_mod._PROCESS_EXIT_TIMEOUT
    acp_mod._CLOSE_TIMEOUT = 0.01
    acp_mod._PROCESS_EXIT_TIMEOUT = 0.01
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    leaked: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: leaked.append(context))
    runner = acp_mod.AcpRunner(AgentSpec(
        key="close-bound", label="Close bound", acp_command=["unused"]))
    runner._conn = object()
    runner._session_id = "session"
    runner._stack = StubbornStack()
    runner._proc = StubbornProcess()
    started = loop.time()
    try:
        await runner.close()
        elapsed = loop.time() - started
        check("close returns after finite transport and process deadlines",
              elapsed < 0.12 and state["terminate"] == 1 and state["kill"] == 1
              and state["wait_cancelled"] >= 1,
              f"elapsed={elapsed:.3f}s, state={state}")
        check("close detaches the session before cleanup completes",
              not runner.live_session() and runner._stack is None
              and runner._proc is None)
    finally:
        release.set()
        await asyncio.wait_for(all_finished.wait(), timeout=0.2)
        await asyncio.sleep(0)
        acp_mod._CLOSE_TIMEOUT = original_close
        acp_mod._PROCESS_EXIT_TIMEOUT = original_exit
        loop.set_exception_handler(previous_handler)
    check("all timed-out close tasks settle after their resource exits",
          state["close_cancelled"] == 1 and state["wait_cancelled"] == 1
          and state["finished"] == 2, str(state))
    check("late close failures are retrieved", not leaked,
          repr([context.get("message") for context in leaked]))


async def test_acp_close_exits_asyncio_run_process() -> None:
    print("\n[9e] ACP close lets the owning asyncio.run process exit")

    script = f"""
import asyncio
import sys

sys.path.insert(0, {str(ROOT)!r})
from leftover.agents import acp_runner as acp_mod
from leftover.config import AgentSpec

acp_mod._CLOSE_TIMEOUT = 0.02
acp_mod._PROCESS_EXIT_TIMEOUT = 0.05

class Cleanup:
    def __init__(self, proc):
        self.proc = proc

    async def aclose(self):
        if self.proc.returncode is not None:
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass

async def main():
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)")
    runner = acp_mod.AcpRunner(AgentSpec(
        key="process-bound", label="Process bound", acp_command=["unused"]))
    runner._conn = object()
    runner._session_id = "session"
    runner._proc = child
    runner._stack = Cleanup(child)
    await runner.close()
    print(f"closed:{{child.returncode}}", flush=True)

asyncio.run(main())
"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(ROOT))
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        stdout, stderr = await proc.communicate()
    check("process-first cleanup reaches asyncio.run shutdown without a pending task",
          not timed_out and proc.returncode == 0
          and stdout.decode().startswith("closed:"),
          f"timed_out={timed_out}, returncode={proc.returncode}, "
          f"stdout={stdout.decode()!r}, stderr={stderr.decode()!r}")


async def test_acp_close_reaps_descendants_holding_stdio() -> None:
    print("\n[9ea] ACP close reaps descendants that inherit its stdio")
    if os.name != "posix":
        check("ACP process-group cleanup is POSIX-only", True)
        return

    from leftover.agents import acp_runner as acp_mod

    def pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def wait_for_record(path: Path) -> tuple[int, int]:
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            if path.exists():
                pid, group = path.read_text().split(":", 1)
                return int(pid), int(group)
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("ACP descendant did not publish its PID")
            await asyncio.sleep(0.01)

    async def wait_for_exit(pid: int) -> bool:
        deadline = asyncio.get_running_loop().time() + 1
        while pid_exists(pid):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    child_script = (
        "import os,pathlib,signal,sys,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text("
        "f'{os.getpid()}:{os.getpgrp()}')\n"
        "while True:\n"
        "    time.sleep(60)\n"
    )
    parent_script = (
        "import os,subprocess,sys\n"
        f"child_script = {child_script!r}\n"
        "subprocess.Popen([sys.executable, '-c', child_script, sys.argv[1]])\n"
        "os.execv(sys.executable, [sys.executable, sys.argv[2]])\n"
    )

    original_exit = acp_mod._PROCESS_EXIT_TIMEOUT
    original_close = acp_mod._CLOSE_TIMEOUT
    acp_mod._PROCESS_EXIT_TIMEOUT = 0.05
    acp_mod._CLOSE_TIMEOUT = 0.2
    proc = None
    group = None
    child_pid = None
    with tempfile.TemporaryDirectory() as tmp:
        record = Path(tmp) / "descendant.pid"
        runner = acp_mod.AcpRunner(AgentSpec(
            key="acp-tree", label="ACP tree", transport="acp",
            acp_command=[
                sys.executable, "-c", parent_script, str(record), MOCK,
            ],
            timeout=2,
        ))
        try:
            await asyncio.wait_for(runner.start(tmp), timeout=3)
            proc = runner._proc
            tree = runner._tree
            child_pid, child_group = await wait_for_record(record)
            group = os.getpgid(proc.pid)
            check("the real ACP adapter owns a separate process group",
                  tree is not None and tree.process_group == proc.pid
                  and group == proc.pid and child_group == group,
                  f"parent={proc.pid}:{group}, child={child_pid}:{child_group}")

            started = asyncio.get_running_loop().time()
            await asyncio.wait_for(runner.close(), timeout=1)
            elapsed = asyncio.get_running_loop().time() - started
            child_exited = await wait_for_exit(child_pid)
            check("ACP close kills the parent and inherited-pipe descendant",
                  elapsed < 0.8 and proc.returncode is not None
                  and child_exited and runner._proc is None
                  and runner._tree is None,
                  f"elapsed={elapsed:.3f}s, parent={proc.returncode}, "
                  f"child_alive={pid_exists(child_pid)}")
        finally:
            acp_mod._PROCESS_EXIT_TIMEOUT = original_exit
            acp_mod._CLOSE_TIMEOUT = original_close
            with contextlib.suppress(Exception):
                await asyncio.wait_for(runner.close(), timeout=0.5)
            if group is not None and group != os.getpgrp():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(group, signal.SIGKILL)
            if child_pid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child_pid, signal.SIGKILL)
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=0.5)


async def test_acp_cancelled_close_keeps_one_full_cleanup() -> None:
    print("\n[9eb] cancelling ACP close keeps its detached SDK cleanup")
    from leftover.agents import acp_runner as acp_mod

    term_sent = asyncio.Event()
    stack_closed = asyncio.Event()
    state = {"terminate": 0, "kill": 0, "stack_calls": 0}

    class SlowProcess:
        returncode = None

        def terminate(self):
            state["terminate"] += 1
            term_sent.set()

        def kill(self):
            state["kill"] += 1
            self.returncode = -signal.SIGKILL

        async def wait(self):
            while self.returncode is None:
                await asyncio.sleep(0)
            return self.returncode

    class Stack:
        async def aclose(self):
            state["stack_calls"] += 1
            stack_closed.set()

    original_exit = acp_mod._PROCESS_EXIT_TIMEOUT
    acp_mod._PROCESS_EXIT_TIMEOUT = 0.03
    process = SlowProcess()
    runner = acp_mod.AcpRunner(AgentSpec(
        key="cancelled-close", label="Cancelled close",
        acp_command=["unused"]))
    runner._conn = object()
    runner._session_id = "session"
    runner._proc = process
    runner._stack = Stack()
    task = asyncio.create_task(runner.close())
    try:
        await term_sent.wait()
        started = asyncio.get_running_loop().time()
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.wait_for(stack_closed.wait(), timeout=0.2)
        check("external cancellation returns while one full cleanup continues",
              isinstance(result[0], asyncio.CancelledError)
              and elapsed < 0.1 and process.returncode == -signal.SIGKILL
              and state == {"terminate": 1, "kill": 1, "stack_calls": 1}
              and not runner.live_session() and runner._proc is None,
              f"elapsed={elapsed:.3f}s, state={state}, "
              f"returncode={process.returncode}")
    finally:
        acp_mod._PROCESS_EXIT_TIMEOUT = original_exit
        if process.returncode is None:
            process.kill()
        await asyncio.gather(task, return_exceptions=True)
        await runner.close()


async def test_acp_close_still_closes_stack_after_process_stop_error() -> None:
    print("\n[9ec] ACP close continues SDK cleanup after process-stop failure")
    from leftover.agents import acp_runner as acp_mod

    state = {"terminate_calls": 0, "stack_calls": 0}
    sentinel_tree = object()

    async def broken_terminate(tree, **kwargs):
        state["terminate_calls"] += 1
        check("ACP cleanup uses the saved process tree",
              tree is sentinel_tree, repr(tree))
        raise RuntimeError("synthetic process-stop failure")

    class Stack:
        async def aclose(self):
            state["stack_calls"] += 1

    original_terminate = acp_mod.terminate_process_tree
    acp_mod.terminate_process_tree = broken_terminate
    try:
        await acp_mod._close_transport_cleanup(
            Stack(), object(), sentinel_tree)
        check("SDK stack closes even when process-tree termination fails",
              state == {"terminate_calls": 1, "stack_calls": 1},
              repr(state))
    finally:
        acp_mod.terminate_process_tree = original_terminate


async def test_acp_filesystem_callbacks_do_not_block_loop() -> None:
    print("\n[9f] ACP filesystem callbacks are bounded and off-loop")
    from leftover.agents import acp_runner as acp_mod

    bridge = acp_mod._Bridge(asyncio.Queue())
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.txt"
        target = Path(tmp) / "nested" / "target.txt"
        source.write_text("one\ntwo\nthree\nfour\nfive\n")
        section = await bridge.read_text_file(
            "session", str(source), line=3, limit=2)
        rest = await bridge.read_text_file(
            "session", str(source), line=4, limit=0)
        await bridge.write_text_file("session", str(target), "written")
        check("line and limit return only the requested file section",
              section.content == "three\nfour\n"
              and rest.content == "four\nfive\n",
              f"section={section.content!r}, rest={rest.content!r}")
        check("threaded writes preserve directory creation and content",
              target.read_text() == "written")

        original_read = acp_mod._read_text_file_sync
        original_write = acp_mod._write_text_file_sync
        slow_release = threading.Event()
        read_started = threading.Event()
        write_started = threading.Event()

        def slow_read(path, line, limit):
            read_started.set()
            slow_release.wait(timeout=0.2)
            return "slow read"

        def slow_write(path, content):
            write_started.set()
            slow_release.wait(timeout=0.2)

        acp_mod._read_text_file_sync = slow_read
        acp_mod._write_text_file_sync = slow_write

        async def ticker() -> None:
            await asyncio.sleep(0.01)
            slow_release.set()

        started = asyncio.get_running_loop().time()
        try:
            await asyncio.gather(
                bridge.read_text_file("session", str(source), line=1, limit=1),
                bridge.write_text_file("session", str(target), "slow"),
                ticker(),
            )
        finally:
            slow_release.set()
            acp_mod._read_text_file_sync = original_read
            acp_mod._write_text_file_sync = original_write
        elapsed = asyncio.get_running_loop().time() - started
        check("slow filesystem calls leave heartbeat and timeout tasks runnable",
              read_started.is_set() and write_started.is_set()
              and elapsed < 0.15,
              f"elapsed={elapsed:.3f}s")

        late_target = Path(tmp) / "late-write.txt"
        late_started = threading.Event()
        late_release = threading.Event()

        def blocked_write(path, content):
            late_started.set()
            late_release.wait()
            Path(path).write_text(content)

        original_write = acp_mod._write_text_file_sync
        original_timeout = acp_mod._FS_IO_TIMEOUT
        acp_mod._write_text_file_sync = blocked_write
        acp_mod._FS_IO_TIMEOUT = 0.03
        write_task = asyncio.create_task(bridge.write_text_file(
            "session", str(late_target), "late content"))
        try:
            deadline = asyncio.get_running_loop().time() + 0.2
            while not late_started.is_set():
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("filesystem write worker did not start")
                await asyncio.sleep(0.001)
            write_error = ""
            try:
                await write_task
            except TimeoutError as exc:
                write_error = str(exc)
            check("running write timeouts disclose their uncertain outcome",
                  "outcome is uncertain" in write_error
                  and "may complete later" in write_error,
                  repr(write_error))

            late_release.set()
            deadline = asyncio.get_running_loop().time() + 0.2
            while (not late_target.exists()
                   and asyncio.get_running_loop().time() < deadline):
                await asyncio.sleep(0.005)
            late_text = (late_target.read_text()
                         if late_target.exists() else "")
            check("the disclosure matches an in-flight write's late side effect",
                  late_text == "late content",
                  "missing late write" if not late_text else repr(late_text))
        finally:
            late_release.set()
            await asyncio.gather(write_task, return_exceptions=True)
            acp_mod._write_text_file_sync = original_write
            acp_mod._FS_IO_TIMEOUT = original_timeout

    check("filesystem execution uses a bounded daemon pool",
          acp_mod._FS_WORKERS.worker_count == acp_mod._FS_WORKER_COUNT
          and acp_mod._FS_WORKERS.queue_limit == acp_mod._FS_QUEUE_LIMIT
          and acp_mod._FS_WORKERS._queue.maxsize == acp_mod._FS_QUEUE_LIMIT)

    script = r'''
import asyncio
import threading
from leftover.agents import acp_runner as mod

def blocked_read(path, line, limit):
    threading.Event().wait()

async def main():
    mod._read_text_file_sync = blocked_read
    mod._FS_IO_TIMEOUT = 0.02
    bridge = mod._Bridge(asyncio.Queue())
    try:
        await bridge.read_text_file("session", "blocked")
    except TimeoutError as exc:
        print(str(exc), flush=True)

asyncio.run(main())
print("asyncio-exited", flush=True)
'''
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(ROOT))
    timed_out = False
    try:
        # Interpreter startup and the ACP import can exceed 0.5s on a busy Mac.
        # The worker call itself still has a 0.02s deadline; this outer bound
        # only detects a process that cannot exit because of the blocked I/O.
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        stdout, stderr = await proc.communicate()
    output = stdout.decode()
    check("permanently blocked filesystem I/O cannot hold asyncio.run open",
          not timed_out and proc.returncode == 0
          and "timed out after 0.02s" in output
          and "asyncio-exited" in output,
          f"timed_out={timed_out}, returncode={proc.returncode}, "
          f"stdout={output!r}, stderr={stderr.decode()!r}")


async def test_acp_prompt_failure_rebuilds_only_next_turn() -> None:
    print("\n[9g] failed ACP prompt retires only its connection generation")
    from leftover.agents import acp_runner as acp_mod

    state = {"spawns": 0, "closes": 0, "late_updates": 0}
    prompts: list[tuple[int, str]] = []
    lifecycle: list[str] = []
    processes: list[asyncio.subprocess.Process] = []
    background: set[asyncio.Task] = set()
    release_late = asyncio.Event()
    first_prompt_started = asyncio.Event()
    release_first_prompt = asyncio.Event()

    class Session:
        def __init__(self, index: int) -> None:
            self.session_id = f"session-{index}"

    class Result:
        stop_reason = "end_turn"

    class Connection:
        def __init__(self, index: int, bridge) -> None:
            self.index = index
            self.bridge = bridge

        async def initialize(self, **kwargs):
            return None

        async def new_session(self, **kwargs):
            return Session(self.index)

        async def prompt(self, session_id, prompt):
            prompts.append((self.index, prompt[0].text))
            if self.index == 1:
                first_prompt_started.set()
                await release_first_prompt.wait()

                async def late_update() -> None:
                    await release_late.wait()
                    state["late_updates"] += 1
                    await self.bridge.queue.put(
                        acp_mod.Event("text", "OLD"))

                task = asyncio.create_task(late_update())
                background.add(task)
                task.add_done_callback(background.discard)
                raise ConnectionError("Connection closed")
            release_late.set()
            await asyncio.sleep(0.01)
            await self.bridge.queue.put(acp_mod.Event("text", "NEW"))
            return Result()

    @contextlib.asynccontextmanager
    async def fake_spawn(bridge, *args, **kwargs):
        state["spawns"] += 1
        index = state["spawns"]
        lifecycle.append(f"spawn:{index}")
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)")
        processes.append(child)
        try:
            yield Connection(index, bridge), child
        finally:
            state["closes"] += 1
            lifecycle.append(f"close:{index}")
            if child.returncode is None:
                child.kill()
                await child.wait()

    original_spawn = acp_mod.spawn_agent_process
    acp_mod.spawn_agent_process = fake_spawn
    runner = acp_mod.AcpRunner(AgentSpec(
        key="rpc-restart", label="RPC restart", acp_command=["fake"],
        timeout=1, acp_idle_timeout=0))
    try:
        first_task = asyncio.create_task(runner.run("first side effect"))
        await first_prompt_started.wait()
        second_task = asyncio.create_task(runner.run("second explicit turn"))
        await asyncio.sleep(0)
        release_first_prompt.set()
        first, second = await asyncio.gather(first_task, second_task)
        check("prompt RPC failure retires before the queued turn rebuilds",
              first.error == "ConnectionError: Connection closed"
              and prompts.count((1, "first side effect")) == 1
              and lifecycle[:3] == ["spawn:1", "close:1", "spawn:2"],
              f"turn={first}, prompts={prompts}, lifecycle={lifecycle}")

        await asyncio.gather(*list(background), return_exceptions=True)
        check("the queued next turn creates a fresh ACP session",
              second.text == "NEW" and runner.live_session()
              and runner.session_id == "session-2"
              and state["spawns"] == 2
              and prompts == [
                  (1, "first side effect"),
                  (2, "second explicit turn"),
              ], f"turn={second}, prompts={prompts}, state={state}")
        check("late output from the failed generation stays isolated",
              state["late_updates"] == 1 and "OLD" not in second.text,
              f"turn={second.text!r}, state={state}")
        check("session rebuild keeps the managed runner as ACP",
              isinstance(runner, acp_mod.AcpRunner))
    finally:
        release_first_prompt.set()
        release_late.set()
        await asyncio.gather(*list(background), return_exceptions=True)
        await runner.close()
        acp_mod.spawn_agent_process = original_spawn
        for child in processes:
            if child.returncode is None:
                child.kill()
                await child.wait()
    check("both ACP generations leave no child process behind",
          state["closes"] == 2
          and all(child.returncode is not None for child in processes),
          f"state={state}, returncodes={[p.returncode for p in processes]}")


async def test_acp_rebuild_failure_uses_exec_fallback() -> None:
    print("\n[9h] failed ACP rebuild installs the same backend's exec runner")
    from leftover import agents as agents_mod
    from leftover.agents import acp_runner as acp_mod

    state = {"spawns": 0, "closes": 0, "prompts": 0}

    class Session:
        session_id = "first-session"

    class Connection:
        def __init__(self, index: int) -> None:
            self.index = index

        async def initialize(self, **kwargs):
            if self.index == 2:
                raise ConnectionError("rebuild handshake failed")

        async def new_session(self, **kwargs):
            return Session()

        async def prompt(self, session_id, prompt):
            state["prompts"] += 1
            raise ConnectionError("Connection reset by peer")

    @contextlib.asynccontextmanager
    async def fake_spawn(*args, **kwargs):
        state["spawns"] += 1
        try:
            yield Connection(state["spawns"]), object()
        finally:
            state["closes"] += 1

    original_spawn = acp_mod.spawn_agent_process
    acp_mod.spawn_agent_process = fake_spawn
    spec = AgentSpec(
        key="rebuild-fallback", label="Rebuild fallback", transport="acp",
        acp_command=["fake"],
        exec_command=[sys.executable, str(ROOT / "tests" / "fake_cli.py")],
        exec_output="json", exec_json_path="result", timeout=2,
        acp_idle_timeout=0)
    pool = AgentPool(Config(
        agents=[spec], default_workdir=str(ROOT), data_dir=str(ROOT)))
    try:
        first = await pool.run(spec, "first")
        acp_runner = pool.peek(spec)
        second = await pool.run(spec, "second")
        check("the failed prompt is not replayed during session retirement",
              first.error == "ConnectionError: Connection reset by peer"
              and state["prompts"] == 1, f"first={first}, state={state}")
        check("a failed next-turn handshake preserves exec fallback",
              isinstance(acp_runner, acp_mod.AcpRunner)
              and isinstance(pool.peek(spec), agents_mod.ExecRunner)
              and second.ok and "exec reply" in second.text,
              f"second={second}, runner={type(pool.peek(spec)).__name__}")
    finally:
        await pool.shutdown()
        acp_mod.spawn_agent_process = original_spawn
    check("failed ACP generations close before fallback ownership changes",
          state == {"spawns": 2, "closes": 2, "prompts": 1}, str(state))


async def test_agent_pool_workdir_gate_preserves_parallel_runs() -> None:
    print("\n[9i] workdir changes are exclusive without serializing agents")
    from leftover import agents as agents_mod

    parallel_release = asyncio.Event()
    switch_release = asyncio.Event()
    parallel_started = {"one": asyncio.Event(), "two": asyncio.Event()}
    after_switch_started = asyncio.Event()
    state = {"active": 0, "max_active": 0}
    starts: list[tuple[str, str]] = []

    class GateRunner(agents_mod.BaseRunner):
        async def start(self, workdir: str) -> None:
            await super().start(workdir)
            starts.append((self.spec.key, workdir))

        async def stream(self, prompt: str, on_event=None):
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            try:
                if prompt == "parallel":
                    parallel_started[self.spec.key].set()
                    await parallel_release.wait()
                elif prompt == "hold switch":
                    await switch_release.wait()
                else:
                    after_switch_started.set()
                yield agents_mod.Event("text", f"{self.spec.key}:{self._workdir}")
                yield agents_mod.Event("done")
            finally:
                state["active"] -= 1

    original_build = agents_mod.build_runner
    agents_mod.build_runner = lambda spec: GateRunner(spec)
    specs = [AgentSpec(key="one", label="One"),
             AgentSpec(key="two", label="Two")]
    with tempfile.TemporaryDirectory() as tmp:
        old_dir = str(Path(tmp) / "old")
        new_dir = str(Path(tmp) / "new")
        Path(old_dir).mkdir()
        Path(new_dir).mkdir()
        pool = AgentPool(Config(
            agents=specs, default_workdir=old_dir, data_dir=tmp))
        try:
            parallel = [
                asyncio.create_task(pool.run(spec, "parallel"))
                for spec in specs
            ]
            await asyncio.gather(*(
                event.wait() for event in parallel_started.values()))
            check("different agent slots still execute concurrently",
                  state["max_active"] == 2, str(state))
            parallel_release.set()
            await asyncio.gather(*parallel)

            holding = asyncio.create_task(pool.run(specs[0], "hold switch"))
            while state["active"] == 0:
                await asyncio.sleep(0)
            switching = asyncio.create_task(pool.set_workdir(new_dir))
            while pool._operations._waiting_writers == 0:
                await asyncio.sleep(0)
            after = asyncio.create_task(pool.run(specs[1], "after switch"))
            await asyncio.sleep(0.02)
            check("a pending workdir switch blocks new operations",
                  not switching.done() and not after_switch_started.is_set())

            switch_release.set()
            await holding
            await switching
            after_turn = await after
            check("the queued operation starts only in the new workdir",
                  after_turn.ok and after_turn.text.endswith(new_dir)
                  and starts[-1] == ("two", new_dir),
                  f"turn={after_turn.text!r}, starts={starts}")
        finally:
            switch_release.set()
            parallel_release.set()
            await pool.shutdown()
            agents_mod.build_runner = original_build


async def test_agent_pool_queue_timeout_is_safe_to_fallback() -> None:
    print("\n[9j] same-agent queue timeout is explicit and not executed")
    from leftover import agents as agents_mod

    occupied = asyncio.Event()
    release = asyncio.Event()
    calls: dict[str, list[str]] = {"first": [], "second": []}

    class QueueRunner(agents_mod.BaseRunner):
        async def stream(self, prompt: str, on_event=None):
            calls[self.spec.key].append(prompt)
            if self.spec.key == "first" and prompt == "occupy":
                occupied.set()
                await release.wait()
            yield agents_mod.Event("text", f"{self.spec.key} answer")
            yield agents_mod.Event("done")

    original_build = agents_mod.build_runner
    original_queue_timeout = agents_mod.RUNNER_QUEUE_TIMEOUT
    agents_mod.build_runner = lambda spec: QueueRunner(spec)
    agents_mod.RUNNER_QUEUE_TIMEOUT = 0.01
    specs = [AgentSpec(key="first", label="First"),
             AgentSpec(key="second", label="Second")]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            agents=specs, default_workdir=tmp, data_dir=tmp,
            routing=Routing(
                strategy="order", order=["first", "second"],
                continuation_guard=False))
        pool = AgentPool(cfg)
        blocker = asyncio.create_task(pool.run(specs[0], "occupy"))
        try:
            await occupied.wait()
            turn, decision = await Router(cfg, pool).run(
                lambda spec: "new work", primary=specs[0],
                ordered_chain=specs, max_attempts=2)
            check("queued work is reported as not executed before fallback",
                  decision.tried == ["first", "second"]
                  and decision.attempts[0].error.startswith(
                      "not executed: First: runner queue wait exceeded")
                  and calls["first"] == ["occupy"],
                  f"calls={calls}, attempts={decision.attempts}")
            check("a different agent can safely handle the unstarted turn",
                  turn.ok and turn.agent is specs[1]
                  and calls["second"] == ["new work"],
                  f"turn={turn}, calls={calls}")
        finally:
            release.set()
            await blocker
            await pool.shutdown()
            agents_mod.build_runner = original_build
            agents_mod.RUNNER_QUEUE_TIMEOUT = original_queue_timeout


async def test_router_does_not_replay_shutdown_interrupted_turn() -> None:
    print("\n[9k] shutdown interruption is terminal but queue timeout is not")

    calls: list[str] = []

    class BoundaryPool:
        async def run(self, spec, prompt, on_event=None):
            calls.append(spec.key)
            if spec.key == "first":
                return Turn(
                    agent=spec,
                    error="not executed: pool shutdown interrupted queued request",
                    meta={
                        "not_executed": True,
                        "shutdown_interrupted": True,
                    },
                )
            return Turn(agent=spec, text="must not run")

    specs = [AgentSpec(key="first", label="First"),
             AgentSpec(key="second", label="Second")]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            agents=specs, default_workdir=tmp, data_dir=tmp,
            routing=Routing(
                strategy="order", order=["first", "second"],
                continuation_guard=False))
        router = Router(cfg, BoundaryPool())
        turn, decision = await router.run(
            lambda spec: "work", primary=specs[0],
            ordered_chain=specs, max_attempts=2)
        check("router stops the cancelled request at the shutdown boundary",
              calls == ["first"] and decision.tried == ["first"]
              and turn.meta.get("shutdown_interrupted") is True
              and decision.attempts[0].timed_out,
              f"calls={calls}, attempts={decision.attempts}")
        check("shutdown interruption does not penalize backend health",
              router.h(specs[0]).consecutive == 0
              and router.h(specs[0]).last_error == "",
              router.h(specs[0]).describe())


async def test_agent_pool_shutdown_interrupts_queued_runs() -> None:
    print("\n[9l] shutdown interrupts queued runs and serializes cancellation")
    from leftover import agents as agents_mod

    started = asyncio.Event()
    run_release = asyncio.Event()
    cancel_entered = asyncio.Event()
    cancel_release = asyncio.Event()
    calls: list[str] = []
    state = {"cancel_calls": 0, "active_cancel": 0,
             "max_cancel": 0, "close_calls": 0}

    class ShutdownRunner(agents_mod.BaseRunner):
        async def stream(self, prompt: str, on_event=None):
            calls.append(prompt)
            if prompt == "active":
                started.set()
                await run_release.wait()
            yield agents_mod.Event("text", prompt)
            yield agents_mod.Event("done")

        async def cancel(self):
            state["cancel_calls"] += 1
            state["active_cancel"] += 1
            state["max_cancel"] = max(
                state["max_cancel"], state["active_cancel"])
            cancel_entered.set()
            try:
                await cancel_release.wait()
                run_release.set()
            finally:
                state["active_cancel"] -= 1

        async def close(self):
            state["close_calls"] += 1

    original_build = agents_mod.build_runner
    agents_mod.build_runner = lambda spec: ShutdownRunner(spec)
    spec = AgentSpec(key="shutdown", label="Shutdown")
    with tempfile.TemporaryDirectory() as tmp:
        pool = AgentPool(Config(
            agents=[spec], default_workdir=tmp, data_dir=tmp))
        active = asyncio.create_task(pool.run(spec, "active"))
        queued = None
        shutdown_one = None
        shutdown_two = None
        try:
            await started.wait()
            queued = asyncio.create_task(pool.run(spec, "queued"))
            lock = pool._runner_lock(spec.key)
            while not getattr(lock, "_waiters", None):
                await asyncio.sleep(0)

            shutdown_one = asyncio.create_task(pool.shutdown())
            await cancel_entered.wait()
            shutdown_two = asyncio.create_task(pool.shutdown())
            await asyncio.sleep(0.01)
            check("overlapping shutdown calls share one cancellation sweep",
                  state["cancel_calls"] == 1 and state["max_cancel"] == 1
                  and not shutdown_two.done(), str(state))

            cancel_release.set()
            active_turn, queued_turn, _, _ = await asyncio.wait_for(
                asyncio.gather(active, queued, shutdown_one, shutdown_two),
                timeout=0.5)
            check("work queued before shutdown is never executed afterward",
                  active_turn.ok and calls == ["active"]
                  and queued_turn.meta.get("not_executed") is True
                  and queued_turn.meta.get("shutdown_interrupted") is True,
                  f"calls={calls}, queued={queued_turn}")
            check("shutdown closes the runner once and completes",
                  state["close_calls"] == 1, str(state))
        finally:
            cancel_release.set()
            run_release.set()
            pending = [task for task in (
                active, queued, shutdown_one, shutdown_two)
                if task is not None and not task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await pool.shutdown()
            agents_mod.build_runner = original_build


async def test_agent_pool_cancel_bypasses_pending_workdir_writer() -> None:
    print("\n[9l] cancel remains available while a workdir writer is pending")
    from leftover import agents as agents_mod

    started = asyncio.Event()
    release = asyncio.Event()
    state = {"cancel_calls": 0, "close_calls": 0}

    class CancelRunner(agents_mod.BaseRunner):
        async def stream(self, prompt: str, on_event=None):
            started.set()
            await release.wait()
            yield agents_mod.Event("text", "finished")
            yield agents_mod.Event("done")

        async def cancel(self):
            state["cancel_calls"] += 1
            release.set()

        async def close(self):
            state["close_calls"] += 1

    original_build = agents_mod.build_runner
    agents_mod.build_runner = lambda spec: CancelRunner(spec)
    spec = AgentSpec(key="cancel-switch", label="Cancel switch")
    with tempfile.TemporaryDirectory() as tmp:
        old_dir = str(Path(tmp) / "old")
        new_dir = str(Path(tmp) / "new")
        Path(old_dir).mkdir()
        Path(new_dir).mkdir()
        pool = AgentPool(Config(
            agents=[spec], default_workdir=old_dir, data_dir=tmp))
        active = asyncio.create_task(pool.run(spec, "active"))
        switching = None
        cancelling = None
        try:
            await started.wait()
            switching = asyncio.create_task(pool.set_workdir(new_dir))
            while pool._operations._waiting_writers == 0:
                await asyncio.sleep(0)
            cancelling = asyncio.create_task(pool.cancel_all())
            active_turn, _, _ = await asyncio.wait_for(
                asyncio.gather(active, switching, cancelling), timeout=0.5)
            check("cancel_all does not queue behind the pending writer",
                  active_turn.ok and state["cancel_calls"] == 1
                  and pool.workdir == new_dir, str(state))
            check("the completed workdir switch closes the old runner",
                  state["close_calls"] == 1, str(state))
        finally:
            release.set()
            pending = [task for task in (active, switching, cancelling)
                       if task is not None and not task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await pool.shutdown()
            agents_mod.build_runner = original_build


async def test_agent_pool_transitions_have_hard_deadlines() -> None:
    print("\n[9m] pool transitions time out without a late writer")
    from leftover import agents as agents_mod

    hold_started = asyncio.Event()
    hold_release = asyncio.Event()
    state = {"cancel_calls": 0, "close_calls": {}}
    instances: dict[str, list] = {"hold": [], "fresh": []}

    class BoundaryRunner(agents_mod.BaseRunner):
        def __init__(self, spec):
            super().__init__(spec)
            self.closed = 0
            instances[spec.key].append(self)

        async def stream(self, prompt: str, on_event=None):
            if self.spec.key == "hold":
                hold_started.set()
                await hold_release.wait()
            yield agents_mod.Event("text", f"{self.spec.key}:{self._workdir}")
            yield agents_mod.Event("done")

        async def cancel(self):
            state["cancel_calls"] += 1

        async def close(self):
            self.closed += 1
            state["close_calls"][self.spec.key] = self.closed

    original_build = agents_mod.build_runner
    original_timeout = agents_mod.POOL_TRANSITION_TIMEOUT
    agents_mod.build_runner = lambda spec: BoundaryRunner(spec)
    agents_mod.POOL_TRANSITION_TIMEOUT = 0.03
    specs = [AgentSpec(key="hold", label="Hold"),
             AgentSpec(key="fresh", label="Fresh")]
    with tempfile.TemporaryDirectory() as tmp:
        old_dir = str(Path(tmp) / "old")
        new_dir = str(Path(tmp) / "new")
        Path(old_dir).mkdir()
        Path(new_dir).mkdir()
        pool = AgentPool(Config(
            agents=specs, default_workdir=old_dir, data_dir=tmp))
        active = asyncio.create_task(pool.run(specs[0], "hold"))
        try:
            await hold_started.wait()
            started = asyncio.get_running_loop().time()
            shutdown_error = None
            try:
                await pool.shutdown()
            except agents_mod._PoolTransitionTimeout as exc:
                shutdown_error = str(exc)
            elapsed = asyncio.get_running_loop().time() - started
            check("shutdown exposes one total deadline when a run ignores cancel",
                  shutdown_error == "agent pool shutdown timed out after 0.03s"
                  and elapsed < 0.15
                  and not pool._shutdown_active
                  and pool._operations._waiting_writers == 0
                  and not pool._lock.locked()
                  and not pool._shutdown_lock.locked(),
                  f"elapsed={elapsed:.3f}s, error={shutdown_error!r}")

            fresh_turn = await asyncio.wait_for(
                pool.run(specs[1], "fresh"), timeout=0.2)
            fresh_runner = pool.peek(specs[1])
            switch_error = None
            try:
                await pool.set_workdir(new_dir)
            except agents_mod._PoolTransitionTimeout as exc:
                switch_error = str(exc)
            check("an unsafe workdir switch fails finitely and keeps the old cwd",
                  fresh_turn.ok
                  and switch_error == (
                      "agent pool workdir switch timed out after 0.03s")
                  and pool.workdir == old_dir
                  and pool._operations._waiting_writers == 0
                  and not pool._lock.locked(),
                  f"turn={fresh_turn}, error={switch_error!r}")

            hold_release.set()
            await active
            await asyncio.sleep(0.05)
            check("timed-out writers cannot later close a newly created runner",
                  pool.peek(specs[1]) is fresh_runner
                  and fresh_runner is not None and fresh_runner.closed == 0
                  and pool.workdir == old_dir,
                  f"fresh={fresh_runner}, cwd={pool.workdir}")

            agents_mod.POOL_TRANSITION_TIMEOUT = original_timeout
            await pool.set_workdir(new_dir)
            check("the pool remains reusable after both transition timeouts",
                  pool.workdir == new_dir and fresh_runner.closed == 1)
        finally:
            hold_release.set()
            await asyncio.gather(active, return_exceptions=True)
            agents_mod.POOL_TRANSITION_TIMEOUT = original_timeout
            await pool.shutdown()
            agents_mod.build_runner = original_build


async def test_agent_pool_close_timeout_only_targets_detached_snapshot() -> None:
    print("\n[9n] close timeout cannot target a replacement runner later")
    from leftover import agents as agents_mod

    close_release = asyncio.Event()
    old_close_finished = asyncio.Event()
    instances: list = []

    class CloseRunner(agents_mod.BaseRunner):
        def __init__(self, spec):
            super().__init__(spec)
            self.index = len(instances) + 1
            self.closed = 0
            instances.append(self)

        async def stream(self, prompt: str, on_event=None):
            yield agents_mod.Event("text", f"runner-{self.index}")
            yield agents_mod.Event("done")

        async def close(self):
            self.closed += 1
            if self.index != 1:
                return
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await close_release.wait()
                old_close_finished.set()

    original_build = agents_mod.build_runner
    original_timeout = agents_mod.POOL_TRANSITION_TIMEOUT
    agents_mod.build_runner = lambda spec: CloseRunner(spec)
    agents_mod.POOL_TRANSITION_TIMEOUT = 0.03
    spec = AgentSpec(key="close-bound", label="Close bound")
    with tempfile.TemporaryDirectory() as tmp:
        pool = AgentPool(Config(
            agents=[spec], default_workdir=tmp, data_dir=tmp))
        try:
            first = await pool.run(spec, "first")
            close_error = None
            started = asyncio.get_running_loop().time()
            try:
                await pool.shutdown()
            except agents_mod._PoolTransitionTimeout as exc:
                close_error = str(exc)
            elapsed = asyncio.get_running_loop().time() - started
            check("stubborn runner close obeys the pool's total boundary",
                  first.text == "runner-1"
                  and close_error == "agent pool shutdown timed out after 0.03s"
                  and elapsed < 0.15 and pool.peek(spec) is None,
                  f"elapsed={elapsed:.3f}s, error={close_error!r}")

            second = await asyncio.wait_for(pool.run(spec, "second"), timeout=0.2)
            replacement = pool.peek(spec)
            close_release.set()
            await asyncio.wait_for(old_close_finished.wait(), timeout=0.2)
            await asyncio.sleep(0)
            check("late cleanup remains bound to the detached old runner",
                  second.text == "runner-2"
                  and replacement is instances[1]
                  and replacement.closed == 0
                  and pool.peek(spec) is replacement,
                  f"instances={[(r.index, r.closed) for r in instances]}")
        finally:
            close_release.set()
            agents_mod.POOL_TRANSITION_TIMEOUT = original_timeout
            await pool.shutdown()
            agents_mod.build_runner = original_build


async def test_agent_pool_fast_close_error_does_not_mask_answer() -> None:
    print("\n[9o] fast close errors do not masquerade as pool timeouts")
    from leftover import agents as agents_mod

    state = {"timeout": 0, "cancelled": 0}

    class BrokenCloseRunner(agents_mod.BaseRunner):
        async def stream(self, prompt: str, on_event=None):
            yield agents_mod.Event("text", "completed answer")
            yield agents_mod.Event("done")

        async def close(self):
            state[self.spec.key] += 1
            if self.spec.key == "timeout":
                raise TimeoutError("runner's own cleanup error")
            raise asyncio.CancelledError

    original_build = agents_mod.build_runner
    agents_mod.build_runner = lambda spec: BrokenCloseRunner(spec)
    specs = [
        AgentSpec(key="timeout", label="Fast timeout error"),
        AgentSpec(key="cancelled", label="Self-cancelled close"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        pool = AgentPool(Config(
            agents=specs, default_workdir=tmp, data_dir=tmp))

        async def run_with_final_cleanup() -> list[Turn]:
            try:
                return await asyncio.gather(*(
                    pool.run(spec, "work") for spec in specs))
            finally:
                await pool.shutdown()

        try:
            turns = await run_with_final_cleanup()
            check("an immediate cleanup error cannot overwrite a good turn",
                  all(turn.ok and turn.text == "completed answer"
                      for turn in turns)
                  and state == {"timeout": 1, "cancelled": 1}
                  and all(pool.peek(spec) is None for spec in specs),
                  f"turns={turns}, state={state}")
        finally:
            agents_mod.build_runner = original_build
            await pool.shutdown()


async def test_agent_pool_external_cancel_restores_transition_state() -> None:
    print("\n[9p] external cancellation restores pool transition state")
    from leftover import agents as agents_mod

    hold_started = asyncio.Event()
    hold_release = asyncio.Event()

    class CancelledTransitionRunner(agents_mod.BaseRunner):
        async def stream(self, prompt: str, on_event=None):
            if self.spec.key == "hold":
                hold_started.set()
                await hold_release.wait()
            yield agents_mod.Event("text", self.spec.key)
            yield agents_mod.Event("done")

        async def cancel(self):
            return None

    original_build = agents_mod.build_runner
    agents_mod.build_runner = lambda spec: CancelledTransitionRunner(spec)
    specs = [AgentSpec(key="hold", label="Hold"),
             AgentSpec(key="fresh", label="Fresh")]
    with tempfile.TemporaryDirectory() as tmp:
        old_dir = str(Path(tmp) / "old")
        new_dir = str(Path(tmp) / "new")
        Path(old_dir).mkdir()
        Path(new_dir).mkdir()
        pool = AgentPool(Config(
            agents=specs, default_workdir=old_dir, data_dir=tmp))
        active = asyncio.create_task(pool.run(specs[0], "hold"))
        try:
            await hold_started.wait()
            switching = asyncio.create_task(pool.set_workdir(new_dir))
            while pool._operations._waiting_writers == 0:
                await asyncio.sleep(0)
            switching.cancel()
            switch_result = await asyncio.gather(
                switching, return_exceptions=True)
            check("cancelling set_workdir removes its writer and lock state",
                  isinstance(switch_result[0], asyncio.CancelledError)
                  and pool.workdir == old_dir
                  and pool._operations._waiting_writers == 0
                  and not pool._operations._writer
                  and not pool._lock.locked())

            shutdown = asyncio.create_task(pool.shutdown())
            while pool._operations._waiting_writers == 0:
                await asyncio.sleep(0)
            shutdown.cancel()
            shutdown_result = await asyncio.gather(
                shutdown, return_exceptions=True)
            check("cancelling shutdown restores epoch and all control locks",
                  isinstance(shutdown_result[0], asyncio.CancelledError)
                  and not pool._shutdown_active
                  and pool._shutdown_epoch % 2 == 0
                  and pool._operations._waiting_writers == 0
                  and not pool._operations._writer
                  and not pool._lock.locked()
                  and not pool._shutdown_lock.locked()
                  and not pool._cancel_lock.locked())

            fresh = await asyncio.wait_for(
                pool.run(specs[1], "fresh"), timeout=0.2)
            check("the pool accepts new work after either external cancellation",
                  fresh.ok and fresh.text == "fresh", repr(fresh))
        finally:
            hold_release.set()
            await asyncio.gather(active, return_exceptions=True)
            await pool.shutdown()
            agents_mod.build_runner = original_build


async def test_acp_abort_is_hard_bounded_and_rotates_queue() -> None:
    print("\n[9q] stubborn ACP abort is bounded and isolates late updates")
    from leftover.agents import acp_runner as acp_mod

    release = asyncio.Event()
    prompt_started = asyncio.Event()
    prompt_finished = asyncio.Event()
    cancel_finished = asyncio.Event()
    close_finished = asyncio.Event()
    state = {"prompt_cancelled": 0, "cancel_cancelled": 0,
             "close_cancelled": 0}

    class Result:
        stop_reason = "end_turn"

    class Connection:
        async def prompt(self, session_id, prompt):
            old_queue = runner._queue
            prompt_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["prompt_cancelled"] += 1
                await release.wait()
                await old_queue.put(acp_mod.Event("text", "OLD"))
                prompt_finished.set()
                raise RuntimeError("late prompt failure")

        async def cancel(self, session_id):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["cancel_cancelled"] += 1
                await release.wait()
                cancel_finished.set()
                raise RuntimeError("late cancel failure")

    class StubbornStack:
        async def aclose(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["close_cancelled"] += 1
                await release.wait()
                close_finished.set()
                raise RuntimeError("late transport close failure")

    class NewConnection:
        async def prompt(self, session_id, prompt):
            await runner._queue.put(acp_mod.Event("text", "NEW"))
            return Result()

    original_rpc = acp_mod._CANCEL_RPC_TIMEOUT
    original_grace = acp_mod._CANCEL_GRACE_TIMEOUT
    original_close = acp_mod._CLOSE_TIMEOUT
    acp_mod._CANCEL_RPC_TIMEOUT = 0.01
    acp_mod._CANCEL_GRACE_TIMEOUT = 0.01
    acp_mod._CLOSE_TIMEOUT = 0.01
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    leaked: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: leaked.append(context))
    runner = acp_mod.AcpRunner(AgentSpec(
        key="stubborn-abort", label="Stubborn abort", acp_command=["unused"],
        timeout=0.01, acp_idle_timeout=0))
    runner._conn = Connection()
    runner._session_id = "old-session"
    runner._stack = StubbornStack()
    old_queue = runner._queue
    started = loop.time()
    try:
        turn = await runner.run("old prompt")
        elapsed = loop.time() - started
        check("prompt, cancel RPC and transport cleanup have a hard bound",
              elapsed < 0.15 and turn.meta.get("timeout_kind") == "turn",
              f"elapsed={elapsed:.3f}s, turn={turn}")
        check("an uncertain abort invalidates its session and queue",
              not runner.live_session() and runner._queue is not old_queue)

        release.set()
        await asyncio.wait_for(asyncio.gather(
            prompt_finished.wait(), cancel_finished.wait(),
            close_finished.wait()), timeout=0.2)
        await asyncio.sleep(0)

        runner._conn = NewConnection()
        runner._session_id = "new-session"
        runner.spec.timeout = 1
        second = await runner.run("new prompt")
        check("late output stays on the abandoned generation queue",
              second.text == "NEW" and old_queue.qsize() > 0,
              f"text={second.text!r}, old_queue={old_queue.qsize()}")
        check("all stubborn cleanup task failures are retrieved",
              not leaked, repr([context.get("message") for context in leaked]))
    finally:
        await runner.close()
        acp_mod._CANCEL_RPC_TIMEOUT = original_rpc
        acp_mod._CANCEL_GRACE_TIMEOUT = original_grace
        acp_mod._CLOSE_TIMEOUT = original_close
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)


async def test_acp_external_cancel_propagates_through_stuck_cleanup() -> None:
    print("\n[9l] external cancellation propagates through stubborn ACP cleanup")
    from leftover.agents import acp_runner as acp_mod

    release = asyncio.Event()
    prompt_started = asyncio.Event()

    class Connection:
        async def prompt(self, session_id, prompt):
            prompt_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise

        async def cancel(self, session_id):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise

    class StubbornStack:
        async def aclose(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise

    original_rpc = acp_mod._CANCEL_RPC_TIMEOUT
    original_grace = acp_mod._CANCEL_GRACE_TIMEOUT
    original_close = acp_mod._CLOSE_TIMEOUT
    acp_mod._CANCEL_RPC_TIMEOUT = 0.01
    acp_mod._CANCEL_GRACE_TIMEOUT = 0.01
    acp_mod._CLOSE_TIMEOUT = 0.01
    runner = acp_mod.AcpRunner(AgentSpec(
        key="cancel-bound", label="Cancel bound", acp_command=["unused"],
        timeout=5, acp_idle_timeout=0))
    runner._conn = Connection()
    runner._session_id = "session"
    runner._stack = StubbornStack()
    task = asyncio.create_task(runner.run("cancel me"))
    try:
        await prompt_started.wait()
        started = asyncio.get_running_loop().time()
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=0.2)
        elapsed = asyncio.get_running_loop().time() - started
        check("outer cancellation is not converted into a normal turn",
              task in done and task.cancelled() and elapsed < 0.15,
              f"done={task.done()}, cancelled={task.cancelled()}, "
              f"elapsed={elapsed:.3f}s")
        check("external cancellation also invalidates the ACP session",
              not runner.live_session())
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.02)
        await runner.close()
        acp_mod._CANCEL_RPC_TIMEOUT = original_rpc
        acp_mod._CANCEL_GRACE_TIMEOUT = original_grace
        acp_mod._CLOSE_TIMEOUT = original_close


async def test_terminal_timeout_does_not_cross_backends() -> None:
    print("\n[9m] completed timeout boundaries do not cross backends")

    class Pool:
        def __init__(self, error: str, timeout_kind: str) -> None:
            self.error = error
            self.timeout_kind = timeout_kind
            self.calls: list[str] = []

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            self.calls.append(spec.key)
            if spec.key == "first":
                return Turn(
                    agent=spec, error=self.error, seconds=180,
                    meta={"timeout_kind": self.timeout_kind})
            return Turn(agent=spec, text="fallback answer")

    agents = [
        AgentSpec(key="first", label="First", fallback=["second"]),
        AgentSpec(key="second", label="Second"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for error, kind in (
                ("timed out after 180s", "turn"),
                ("ACP idle timed out after 30s without an update", "idle")):
            pool = Pool(error, kind)
            cfg = Config(
                agents=agents, data_dir=tmp,
                routing=Routing(strategy="order", order=["first", "second"]))
            turn, decision = await Router(cfg, pool).run(
                lambda spec: "work", primary=agents[0], max_attempts=2)
            check(f"{kind} timeout stops before another vendor",
                  pool.calls == ["first"] and turn.error == error
                  and decision.tried == ["first"],
                  f"calls={pool.calls}, tried={decision.tried}")

        pool = Pool("connection reset by peer", "")
        cfg = Config(
            agents=agents, data_dir=tmp,
            routing=Routing(strategy="order", order=["first", "second"]))
        turn, decision = await Router(cfg, pool).run(
            lambda spec: "work", primary=agents[0], max_attempts=2)
        check("quick transient failures still use fallback",
              pool.calls == ["first", "second"] and turn.ok
              and decision.chosen is agents[1],
              f"calls={pool.calls}, chosen={getattr(decision.chosen, 'key', None)}")


async def test_continuation_guard_prefixes_failover() -> None:
    print("\n[9i] failover prompt warns the next agent about dirty files")

    class Pool:
        def __init__(self) -> None:
            self.prompts: list[tuple[str, str]] = []

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            self.prompts.append((spec.key, prompt))
            if spec.key == "first":
                return Turn(agent=spec, error="You've hit your weekly limit")
            return Turn(agent=spec, text="ok")

    agents = [
        AgentSpec(key="first", label="First"),
        AgentSpec(key="second", label="Second"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        pool = Pool()
        cfg = Config(
            agents=agents, data_dir=tmp,
            routing=Routing(strategy="order", order=["first", "second"]))
        turn, decision = await Router(cfg, pool).run(
            lambda spec: "work", primary=agents[0], max_attempts=2)
        check("first attempt is the bare prompt",
              pool.prompts[0] == ("first", "work"), str(pool.prompts))
        check("second attempt gets usher's dirty-tree notice",
              turn.ok and decision.chosen is agents[1]
              and pool.prompts[1][0] == "second"
              and pool.prompts[1][1].startswith(CONTINUATION_GUARD)
              and pool.prompts[1][1].endswith("work"),
              str(pool.prompts[1]))

        pool = Pool()
        cfg.routing.continuation_guard = False
        turn, decision = await Router(cfg, pool).run(
            lambda spec: "work", primary=agents[0], max_attempts=2)
        check("toml can turn the notice off",
              [prompt for _, prompt in pool.prompts] == ["work", "work"]
              and turn.ok, str(pool.prompts))


async def test_two_round_debate_parallel_context() -> None:
    print("\n[10] two-round debate overlaps sides and carries round context")

    class RoundPool:
        def __init__(self) -> None:
            self.active = {1: 0, 2: 0}
            self.max_active = {1: 0, 2: 0}
            self.prompts: dict[tuple[str, int], str] = {}

        def peek(self, spec: AgentSpec):
            return None

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            if "neutral judge" in prompt:
                return Turn(agent=spec, text="JUDGE_VERDICT", seconds=0.001)

            side = "FOR" if "arguing FOR" in prompt else "AGAINST"
            round_no = 1 if "Round 1 of 2" in prompt else 2
            self.prompts[(side, round_no)] = prompt
            self.active[round_no] += 1
            self.max_active[round_no] = max(
                self.max_active[round_no], self.active[round_no])
            try:
                await asyncio.sleep(0.03)
            finally:
                self.active[round_no] -= 1
            return Turn(
                agent=spec,
                text=f"{side}_R{round_no}_ARGUMENT",
                seconds=0.03,
            )

    agents = [
        AgentSpec(key="pro", label="Pro"),
        AgentSpec(key="con", label="Con"),
        AgentSpec(key="judge", label="Judge"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        pool = RoundPool()
        cfg = Config(
            agents=agents,
            data_dir=tmp,
            debate_rounds=2,
            debate_turn_timeout=1,
            routing=Routing(strategy="order", order=[a.key for a in agents]),
        )
        orch = Orchestrator(cfg, pool, Router(cfg, pool))
        turns = await orch.execute(
            Plan("debate", "parallel rounds", agents, {}), None)

    check("FOR and AGAINST overlap independently in both rounds",
          pool.max_active == {1: 2, 2: 2}, str(pool.max_active))
    round_two = [
        pool.prompts[("FOR", 2)],
        pool.prompts[("AGAINST", 2)],
    ]
    round_one_counts = [
        (prompt.count("FOR_R1_ARGUMENT"),
         prompt.count("AGAINST_R1_ARGUMENT"))
        for prompt in round_two
    ]
    check("both second-round advocates see both first-round arguments once",
          round_one_counts == [(1, 1), (1, 1)],
          str(round_one_counts))
    check("debate_rounds=2 yields two labeled rounds and one verdict",
          [(turn.meta.get("discussion_role"),
            turn.meta.get("discussion_round")) for turn in turns]
          == [
              ("FOR", 1), ("AGAINST", 1),
              ("FOR", 2), ("AGAINST", 2),
              ("JUDGE", None),
          ],
          repr([turn.meta for turn in turns]))
    check("debate permits named read-only evidence but still forbids changes",
          all("explicitly asks you to inspect named repository files" in prompt
              and "only read-only file or search tools" in prompt
              and "Never edit files, implement changes" in prompt
              for prompt in round_two))


async def test_debate_cancellation_drains_warmups() -> None:
    print("\n[10b] cancelling debate cancels speculative warmups promptly")

    class WarmPool:
        def __init__(self, count: int) -> None:
            self.count = count
            self.started = 0
            self.cancelled = 0
            self.all_started = asyncio.Event()
            self.all_cancelled = asyncio.Event()

        async def prepare(self, spec: AgentSpec) -> None:
            self.started += 1
            if self.started == self.count:
                self.all_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                if self.cancelled == self.count:
                    self.all_cancelled.set()
                raise

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    agents = [
        AgentSpec(key="pro", label="Pro"),
        AgentSpec(key="con", label="Con"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        pool = WarmPool(len(agents))
        cfg = Config(
            agents=agents,
            data_dir=tmp,
            debate_turn_timeout=180,
            routing=Routing(strategy="order", order=[a.key for a in agents]),
        )
        orch = Orchestrator(cfg, pool, Router(cfg, pool))
        task = asyncio.create_task(orch.execute(
            Plan("debate", "cancel me", agents, {"rounds": "1"}), None))
        await asyncio.wait_for(pool.all_started.wait(), timeout=0.2)
        started = asyncio.get_running_loop().time()
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.5)
        elapsed = asyncio.get_running_loop().time() - started
        cancelled = False
        if task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                cancelled = True
        check("cancelled debate exits below the warmup timeout",
              cancelled and elapsed < 0.35,
              f"elapsed={elapsed:.3f}s, done={task in done}")
        check("every speculative warmup receives cancellation",
              pool.all_cancelled.is_set() and pool.cancelled == len(agents),
              f"cancelled={pool.cancelled}")

    class RaisingWarmPool:
        async def prepare(self, spec: AgentSpec) -> None:
            raise RuntimeError(f"{spec.key} warmup failed")

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            return Turn(agent=spec, text="ok")

    with tempfile.TemporaryDirectory() as tmp:
        pool = RaisingWarmPool()
        cfg = Config(
            agents=agents,
            data_dir=tmp,
            debate_turn_timeout=1,
            routing=Routing(strategy="order", order=[a.key for a in agents]),
        )
        turns = await Orchestrator(cfg, pool, Router(cfg, pool)).execute(
            Plan("debate", "finish", agents, {"rounds": "1"}), None)
        check("normal debate consumes warmup exceptions",
              len(turns) == 2 and all(turn.ok for turn in turns))


async def test_managed_prepare_falls_back_once() -> None:
    print("\n[11] managed prepare and run share one ACP startup attempt")
    from leftover import agents as agents_mod

    state = {"starts": 0, "closed": 0}

    class FailingRunner(agents_mod.BaseRunner):
        async def start(self, workdir: str) -> None:
            await super().start(workdir)
            state["starts"] += 1
            await asyncio.sleep(0.02)
            raise ConnectionError("handshake failed")

        async def close(self) -> None:
            state["closed"] += 1

    original = agents_mod.build_runner
    agents_mod.build_runner = lambda spec: FailingRunner(spec)
    spec = AgentSpec(
        key="prepared", label="Prepared", transport="acp",
        acp_command=["broken"],
        exec_command=[sys.executable, str(ROOT / "tests" / "fake_cli.py")],
        exec_output="json", exec_json_path="result", timeout=5,
    )
    with tempfile.TemporaryDirectory() as tmp:
        pool = AgentPool(Config(
            agents=[spec], data_dir=tmp, default_workdir=str(ROOT)))
        try:
            prepared, turn = await asyncio.gather(
                pool.prepare(spec), pool.run(spec, "prepared fallback"))
            check("prepare plus run attempts the ACP handshake once",
                  state == {"starts": 1, "closed": 1}, str(state))
            check("prepare leaves the managed exec fallback installed",
                  isinstance(prepared, agents_mod.ExecRunner)
                  and pool.peek(spec) is prepared)
            check("the concurrent turn runs on the installed fallback",
                  turn.ok and "exec reply" in turn.text, turn.short())
        finally:
            agents_mod.build_runner = original
            await pool.shutdown()


async def test_debate_walks_distinct_installed_spares() -> None:
    print("\n[12] debate fallback walks distinct installed spares")

    class SparePool:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def peek(self, spec: AgentSpec):
            return None

        async def run(self, spec: AgentSpec, prompt: str, on_event=None) -> Turn:
            role = "JUDGE" if "neutral judge" in prompt else (
                "FOR" if "arguing FOR" in prompt else "AGAINST")
            self.calls.append((spec.key, role))
            if spec.key in {"pro", "con", "bad-spare"}:
                return Turn(agent=spec, error="connection reset by peer")
            return Turn(agent=spec, text=f"{spec.key} handled {role}")

    installed = [sys.executable]
    agents = [
        AgentSpec(key="pro", label="Pro", interactive_command=installed),
        AgentSpec(key="con", label="Con", interactive_command=installed),
        AgentSpec(key="judge", label="Judge", interactive_command=installed),
        AgentSpec(key="missing", label="Missing"),
        AgentSpec(key="bad-spare", label="Bad spare", interactive_command=installed),
        AgentSpec(key="good-for", label="Good for", interactive_command=installed),
        AgentSpec(key="good-against", label="Good against",
                  interactive_command=installed),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        pool = SparePool()
        cfg = Config(
            agents=agents, data_dir=tmp, debate_turn_timeout=1,
            routing=Routing(strategy="order", order=[a.key for a in agents]),
        )
        orch = Orchestrator(cfg, pool, Router(cfg, pool))
        turns = await orch.execute(
            Plan("debate", "ship", agents[:3], {"rounds": "1"}), None)

    check("an uninstalled spare is skipped without a call",
          all(key != "missing" for key, _ in pool.calls), str(pool.calls))
    check("a failed spare is not retried for the opposing role",
          pool.calls.count(("bad-spare", "FOR")) == 1
          and ("bad-spare", "AGAINST") not in pool.calls,
          str(pool.calls))
    check("fallback continues to distinct healthy spares",
          [(turn.agent.key, turn.meta.get("discussion_role")) for turn in turns]
          == [
              ("good-for", "FOR"),
              ("good-against", "AGAINST"),
              ("judge", "JUDGE"),
          ],
          repr([(turn.agent.key, turn.error) for turn in turns]))


def test_debate_panel_and_explicit_names() -> None:
    print("\n[13] configured judge and explicit debate names are consistent")
    installed = [sys.executable]
    agents = [
        AgentSpec(key=key, label=key.title(), interactive_command=installed)
        for key in ("claude", "gpt", "grok", "cursor")
    ]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            agents=agents, data_dir=tmp, debate_judge_key="cursor",
            routing=Routing(plan_key="claude", coding_keys=["gpt", "grok"]),
        )
        orch = Orchestrator(cfg, object())
        check("judge_key works even outside the discussion coding panel",
              [spec.key for spec in orch.debate_panel()]
              == ["claude", "gpt", "cursor"])
        plan = orch.parse(
            "/debate @grok @gpt @cursor inspect docs/design.md", in_group=False)
        check("explicit debate names preserve their order",
              plan is not None
              and [spec.key for spec in plan.agents]
              == ["grok", "gpt", "cursor"])
        check("explicit debate mentions are removed from the proposition",
              plan is not None and plan.prompt == "inspect docs/design.md",
              "" if plan is None else repr(plan.prompt))


async def main() -> int:
    test_classification()
    test_config_parallel_bound()
    await test_success_result_refusal_boundary()
    test_codex_probe()
    test_keychain_write_keeps_the_token_off_argv()
    test_claude_usage_parse()
    test_grok_billing_parse()
    test_sub2api_codex_probe()
    test_cursor_usage_parse()
    test_quota_rhythm()
    test_ledger()
    await test_quota_fallback()
    await test_quota_probe_preserves_concurrent_observed_limit()
    await test_fresh_quota_replaces_stale_observed_without_reset()
    await test_concurrent_probes_do_not_revive_stale_observed_limit()
    await test_circuit_breaker()
    await test_recovery_and_auto()
    await test_group_substitution()
    await test_parallel_fallback_shares_one_ranking()
    await test_group_role_reservations()
    await test_debate_turn_timeout_is_bounded()
    await test_acp_idle_timeout_tracks_all_updates()
    await test_acp_idle_timeout_cleans_up_silence()
    await test_acp_internal_activity_does_not_extend_idle_deadline()
    await test_agent_pool_start_timeout_is_a_hard_boundary()
    await test_acp_close_is_a_hard_boundary()
    await test_acp_close_exits_asyncio_run_process()
    await test_acp_close_reaps_descendants_holding_stdio()
    await test_acp_cancelled_close_keeps_one_full_cleanup()
    await test_acp_close_still_closes_stack_after_process_stop_error()
    await test_acp_filesystem_callbacks_do_not_block_loop()
    await test_acp_prompt_failure_rebuilds_only_next_turn()
    await test_acp_rebuild_failure_uses_exec_fallback()
    await test_agent_pool_workdir_gate_preserves_parallel_runs()
    await test_agent_pool_queue_timeout_is_safe_to_fallback()
    await test_router_does_not_replay_shutdown_interrupted_turn()
    await test_agent_pool_shutdown_interrupts_queued_runs()
    await test_agent_pool_cancel_bypasses_pending_workdir_writer()
    await test_agent_pool_transitions_have_hard_deadlines()
    await test_agent_pool_close_timeout_only_targets_detached_snapshot()
    await test_agent_pool_fast_close_error_does_not_mask_answer()
    await test_agent_pool_external_cancel_restores_transition_state()
    await test_acp_abort_is_hard_bounded_and_rotates_queue()
    await test_acp_external_cancel_propagates_through_stuck_cleanup()
    await test_terminal_timeout_does_not_cross_backends()
    await test_continuation_guard_prefixes_failover()
    await test_two_round_debate_parallel_context()
    await test_debate_cancellation_drains_warmups()
    await test_managed_prepare_falls_back_once()
    await test_debate_walks_distinct_installed_spares()
    test_debate_panel_and_explicit_names()
    ok = all(RESULTS)
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
