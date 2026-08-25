"""Lag + waste scoring for subscription windows.

Pick the coding backend whose quota is most *behind schedule* (lag) and
needs the highest catch-up rate before reset (waste). A fresh short window
starts on schedule instead of immediately starving an overdue weekly pool;
as it falls behind or approaches reset, its score rises quickly.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .quota import ESTIMATED, Quota, Window

_HOURS = re.compile(r"(?:^|[^\d])(\d+(?:\.\d+)?)h\b", re.I)
_DAYS = re.compile(r"(?:^|[^\d])(\d+(?:\.\d+)?)d\b", re.I)


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


def _elapsed_frac(window: Window, now: float) -> float:
    if (window.started_at is not None and window.resets_at is not None
            and window.resets_at > window.started_at):
        elapsed = ((now - window.started_at)
                   / (window.resets_at - window.started_at))
        return min(1.0, max(0.0, elapsed))
    length = window_length_seconds(window)
    if length and window.resets_at is not None:
        left = max(0.0, window.resets_at - now)
        return min(1.0, max(0.0, 1.0 - left / length))
    # Nothing to run a clock against: treat the window as half elapsed so it
    # neither looks overdue nor immune.
    return 0.5


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


def score_window(window: Window, now: float | None = None,
                 lag_weight: float = 0.5,
                 waste_weight: float = 1.0) -> WindowScore:
    now = time.time() if now is None else now
    used = min(1.0, max(0.0, window.used_percent / 100.0))
    hours = _hours_left(window, now)
    lag = max(0.0, _elapsed_frac(window, now) - used)
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


def score_quota(key: str, quota: Quota, now: float | None = None,
                lag_weight: float = 0.5,
                waste_weight: float = 1.0) -> AgentScore:
    """One number per agent: the window that most wants to be spent."""
    now = time.time() if now is None else now
    live = [w for w in quota.windows
            if w.name != "extra"
            and (w.resets_at is None or w.resets_at > now)]
    if not live:
        return AgentScore(key=key, lag=0.0, waste=0.0, total=0.0,
                          source=quota.best_source, detail="no live window",
                          windows=[])
    scored = [score_window(w, now, lag_weight, waste_weight) for w in live]
    best = max(scored, key=lambda s: s.total)
    return AgentScore(
        key=key,
        lag=best.lag,
        waste=best.waste,
        total=best.total,
        source=quota.best_source,
        detail=(f"{best.name} {best.used_percent:.0f}% used, "
                f"{best.hours_left:.1f}h left, lag {best.lag:.2f} "
                f"waste {best.waste:.3f} ({quota.best_source})"),
        windows=scored,
    )
