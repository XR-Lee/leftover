"""Lag + waste scoring for subscription windows.

Pick the coding backend whose *allocation* window is most behind schedule
(lag) and needs the highest catch-up rate before reset (waste). Weekly and
monthly pools are allocation; 5h/session is a rate limit. Ranking uses the
allocation number. A full session window is skipped; a behind session only
breaks ties when allocation scores match.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .quota import ESTIMATED, Quota, Window

_HOURS = re.compile(r"(?:^|[^\d])(\d+(?:\.\d+)?)h\b", re.I)
_DAYS = re.compile(r"(?:^|[^\d])(\d+(?:\.\d+)?)d\b", re.I)
_PACE_TOLERANCE = 0.005
_PLAN_TIE_DECIMALS = 3
_SESSION_FULL = 100.0


def window_length_seconds(window: Window) -> float | None:
    """Best-effort length of one bucket, so lag has a clock to run against."""
    name = window.name.lower()
    if "week" in name:
        return 7 * 86400
    if "month" in name:
        return 30 * 86400
    if "5h" in name or "session" in name:
        return 5 * 3600
    if m := _HOURS.search(name):
        return float(m.group(1)) * 3600
    if m := _DAYS.search(name):
        return float(m.group(1)) * 86400
    return None


def _hours_left(window: Window, now: float) -> float:
    if window.resets_at is not None:
        return max((window.resets_at - now) / 3600.0, 1.0 / 60.0)
    length = window_length_seconds(window)
    if length:
        return max(length / 3600.0, 1.0 / 60.0)
    return 30 * 24.0


def _elapsed_frac(window: Window, now: float) -> float | None:
    if (window.started_at is not None and window.resets_at is not None
            and window.resets_at > window.started_at):
        elapsed = ((now - window.started_at)
                   / (window.resets_at - window.started_at))
        return min(1.0, max(0.0, elapsed))
    length = window_length_seconds(window)
    if length and window.resets_at is not None:
        left = max(0.0, window.resets_at - now)
        return min(1.0, max(0.0, 1.0 - left / length))
    # A reported percentage without a reset clock is still useful to display,
    # but it cannot tell us whether this window is ahead or behind schedule.
    return None


@dataclass
class WindowScore:
    name: str
    lag: float
    waste: float
    total: float
    used_percent: float
    hours_left: float


@dataclass
class AgentScore:
    key: str
    lag: float
    waste: float
    total: float
    source: str
    detail: str
    windows: list[WindowScore]
    focus: str = ""
    ahead: float = 0.0
    session_total: float = 0.0
    session_blocked: bool = False


def score_window(window: Window, now: float | None = None,
                 lag_weight: float = 0.5,
                 waste_weight: float = 1.0) -> WindowScore:
    now = time.time() if now is None else now
    used = min(1.0, max(0.0, window.used_percent / 100.0))
    hours = _hours_left(window, now)
    elapsed = _elapsed_frac(window, now)
    lag = 0.0 if elapsed is None else max(0.0, elapsed - used)
    # Catch-up rate, not the whole unused pool divided by time. Otherwise a
    # just-reset short window is permanently urgent and starves weekly pools
    # that are already behind schedule. Guessed budgets still get no waste.
    waste = 0.0 if window.source == ESTIMATED else lag / hours
    return WindowScore(
        name=window.name,
        lag=lag,
        waste=waste,
        total=lag_weight * lag + waste_weight * waste,
        used_percent=window.used_percent,
        hours_left=hours,
    )


def window_role(name: str) -> str:
    """session = 5h rate limit. plan = weekly/monthly allocation. other = slices."""
    n = name.lower().strip()
    if "5h" in n or n == "session" or n.startswith("session"):
        return "session"
    if "week" in n or n == "7d" or n.startswith("7d"):
        return "plan"
    if n in ("monthly", "month"):
        return "plan"
    if n.startswith("monthly "):
        return "other"
    if "month" in n:
        return "plan"
    return "other"


def _plan_rank(window: Window) -> tuple[int, int]:
    n = window.name.lower()
    if n in ("weekly", "7d", "week"):
        return (0, 0)
    if "week" in n or n.startswith("7d"):
        return (0, 1)
    if n in ("monthly", "month"):
        return (1, 0)
    if "month" in n and not n.startswith("monthly "):
        return (1, 1)
    if window_role(window.name) == "session":
        return (3, 0)
    return (2, 0)


def pick_plan(windows: list[Window]) -> Window | None:
    """Allocation window: weekly, else monthly, else the only remaining bucket.

    A 5h window is the plan only when the vendor did not publish a
    weekly/monthly pool. Cursor product slices (`monthly auto`) never win.
    """
    live = [w for w in windows if w.name != "extra"]
    if not live:
        return None
    non_session = [w for w in live if window_role(w.name) != "session"]
    pool = non_session or live
    return min(pool, key=_plan_rank)


def rank_tuple(score: AgentScore, priority: int = 0) -> tuple:
    """Sort key: skip a full 5h, then plan ahead, then plan score, then 5h tie."""
    return (
        score.session_blocked,
        score.ahead > 0.0,
        -round(score.total, _PLAN_TIE_DECIMALS),
        -score.session_total,
        score.ahead,
        priority,
    )


def _pace_ahead(window: Window, now: float) -> tuple[float, float] | None:
    elapsed = _elapsed_frac(window, now)
    if elapsed is None:
        return None
    used = min(1.0, max(0.0, window.used_percent / 100.0))
    if used > elapsed + _PACE_TOLERANCE:
        return (used - elapsed, elapsed)
    return None


def score_quota(key: str, quota: Quota, now: float | None = None,
                lag_weight: float = 0.5,
                waste_weight: float = 1.0) -> AgentScore:
    """One number per agent, from the allocation window it consumes."""
    now = time.time() if now is None else now
    live = [w for w in quota.windows
            if w.name != "extra"
            and (w.resets_at is None or w.resets_at > now)]
    if not live:
        return AgentScore(key=key, lag=0.0, waste=0.0, total=0.0,
                          source=quota.best_source, detail="no live window",
                          windows=[])
    scored = [score_window(w, now, lag_weight, waste_weight) for w in live]
    plan = pick_plan(live)
    if plan is None:
        return AgentScore(key=key, lag=0.0, waste=0.0, total=0.0,
                          source=quota.best_source, detail="no live window",
                          windows=scored)
    best = next(s for s, w in zip(scored, live) if w is plan)
    sessions = [
        (w, s) for w, s in zip(live, scored)
        if window_role(w.name) == "session" and w is not plan
    ]
    session_total = max((s.total for _w, s in sessions), default=0.0)
    session_blocked = any(
        w.used_percent >= _SESSION_FULL for w, _s in sessions)

    # A turn spends the allocation window. Gate on that window only — a
    # behind 5h must not zero an agent whose weekly/monthly pool is overdue.
    ahead = _pace_ahead(plan, now)
    if ahead is not None:
        gap, elapsed = ahead
        detail = (f"{plan.name} {plan.used_percent:.0f}% used, "
                  f"{elapsed * 100.0:.1f}% elapsed, ahead of pace "
                  f"({quota.best_source})")
        if session_blocked:
            detail += "; session full"
        return AgentScore(
            key=key,
            lag=0.0,
            waste=0.0,
            total=0.0,
            source=quota.best_source,
            detail=detail,
            windows=scored,
            focus=plan.name,
            ahead=gap,
            session_total=session_total,
            session_blocked=session_blocked,
        )

    if _elapsed_frac(plan, now) is None:
        detail = (f"{best.name} {best.used_percent:.0f}% used, no reset clock, "
                  f"routing urgency 0 ({quota.best_source})")
    else:
        detail = (f"{best.name} {best.used_percent:.0f}% used, "
                  f"{best.hours_left:.1f}h left, lag {best.lag:.2f} "
                  f"waste {best.waste:.3f} ({quota.best_source})")
    if session_blocked:
        full = next(w for w, _s in sessions if w.used_percent >= _SESSION_FULL)
        detail += f"; {full.name} {full.used_percent:.0f}% used, skip"
    return AgentScore(
        key=key,
        lag=best.lag,
        waste=best.waste,
        total=best.total,
        source=quota.best_source,
        detail=detail,
        windows=scored,
        focus=best.name,
        session_total=session_total,
        session_blocked=session_blocked,
    )
