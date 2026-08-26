"""Quota rhythm view: calendar vs usage bars, same-window deltas.

▾behind / ▴ahead = used vs calendar elapsed.
↑ = same-window increase; "new window from 0" = reset, then usage from empty.
widening / narrowing = |calendar - used| vs the previous snapshot of that window.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .config import AgentSpec
from .quota import ESTIMATED, Quota, Window
from .score import window_length_seconds

BAR_WIDTH = 16
BEHIND = "\u25be" "behind"
AHEAD = "\u25b4" "ahead"
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
_LABELS = {
    "weekly": "7d",
    "7d": "7d",
    "5h": "5h",
    "session": "5h",
    "monthly": "monthly",
    "monthly auto": "Models",
    "monthly api": "Other api%",
    "extra": "extra",
}


def _tz(name: str = ""):
    """Named zone if the config asks for one, otherwise this machine's."""
    if not name.strip():
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return datetime.now().astimezone().tzinfo


def _tz_label(tz) -> str:
    key = str(getattr(tz, "key", "") or "")
    if key == "Europe/London" or key.endswith("/London"):
        return "London"
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    return now.tzname() or "local"


def fmt_pct(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{round(value):.0f}%"
    return f"{value:.1f}%"


def fmt_usd(value: float) -> str:
    if abs(value - round(value)) < 0.005:
        return f"${value:.0f}"
    return f"${value:.2f}"


def fmt_req(n: int) -> str:
    if n >= 1000:
        k = n / 1000
        text = f"{k:.1f}K" if abs(k - round(k)) >= 0.05 else f"{round(k)}K"
        return f"{text} req"
    return f"{n} req"


def bar(percent: float, width: int = BAR_WIDTH) -> str:
    filled = int(round(min(100.0, max(0.0, percent)) / 100.0 * width))
    filled = min(width, max(0, filled))
    return "█" * filled + "░" * (width - filled)


def _when(ts: float, tz, *, with_year: bool = False) -> str:
    dt = datetime.fromtimestamp(ts, tz)
    month = _MONTHS[dt.month - 1]
    if with_year:
        return f"{dt.day} {month} {dt.year} {dt:%H:%M}"
    return f"{dt.day} {month} {dt:%H:%M}"


def fmt_left(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90 * 60:
        return f"{round(seconds / 60)}m"
    days, rem = divmod(seconds, 86400)
    hours = int(rem // 3600)
    if days:
        return f"{int(days)}d {hours}h" if hours else f"{int(days)}d"
    return f"{seconds / 3600:.1f}h"


def calendar_pct(window: Window, now: float) -> float | None:
    start, end = window.started_at, window.resets_at
    if start is None and end is not None:
        length = window_length_seconds(window)
        if length:
            start = end - length
    if start is None or end is None or end <= start:
        return None
    return min(100.0, max(0.0, (now - start) / (end - start) * 100.0))


def label_of(window: Window) -> str:
    return _LABELS.get(window.name, window.name)


def same_window(a: Window, b: Window) -> bool:
    if a.name != b.name:
        return False
    if a.resets_at is None or b.resets_at is None:
        return a.resets_at == b.resets_at
    return abs(a.resets_at - b.resets_at) < 1800


def just_reset(window: Window, now: float) -> bool:
    if window.source == ESTIMATED:
        return False
    # Any reported usage is a live percentage, including a 1% window
    # that just refreshed. Only an empty early window is a footnote.
    if window.used_percent > 0:
        return False
    cal = calendar_pct(window, now)
    if cal is not None:
        return cal < 8
    return False


def _prev_window(prev: Quota | None, window: Window) -> Window | None:
    if prev is None:
        return None
    for other in prev.windows:
        if same_window(other, window):
            return other
    return None


def pace_tags(window: Window, prev: Window | None, *,
              now: float, prev_now: float | None,
              money: bool = False) -> list[str]:
    tags: list[str] = []
    cal = calendar_pct(window, now)
    if cal is not None:
        if window.used_percent < cal - 0.5:
            tags.append(BEHIND)
        elif window.used_percent > cal + 0.5:
            tags.append(AHEAD)
    if prev is None:
        return tags
    if not same_window(window, prev):
        tags.append("new window from 0")
        return tags
    if money and window.used_usd is not None and prev.used_usd is not None:
        delta = window.used_usd - prev.used_usd
        if delta >= 0.005:
            tags.append(f"↑{fmt_usd(delta)}")
    else:
        delta = window.used_percent - prev.used_percent
        if delta >= 0.05:
            tags.append("↑" + fmt_pct(delta))
    if cal is None or prev_now is None:
        return tags
    prev_cal = calendar_pct(prev, prev_now)
    if prev_cal is None:
        return tags
    gap = abs(cal - window.used_percent)
    prev_gap = abs(prev_cal - prev.used_percent)
    if gap - prev_gap > 0.15:
        tags.append("widening")
    elif prev_gap - gap > 0.15:
        tags.append("narrowing")
    return tags


def _join(parts: Iterable[str | None]) -> str:
    return " · ".join(p for p in parts if p)


def _keep_window(window: Window, now: float) -> bool:
    expired = window.resets_at is not None and window.resets_at <= now
    if expired and not just_reset(window, now):
        return False
    return True


def _real(quota: Quota, now: float) -> list[Window]:
    vendor = [
        window for window in quota.windows
        if window.source != ESTIMATED and _keep_window(window, now)
    ]
    if vendor:
        return vendor
    return [
        window for window in quota.windows
        if window.source == ESTIMATED and _keep_window(window, now)
    ]


def _display_title(quota: Quota, fallback: str, windows: list[Window]) -> str:
    title = quota.title or fallback
    if windows and all(window.source == ESTIMATED for window in windows):
        if "estimated" not in title.lower():
            return f"{title} · estimated local"
    return title


def _primary(windows: list[Window], now: float) -> Window | None:
    live = [w for w in windows if not just_reset(w, now)]
    pool = live or windows
    rank = ("weekly", "7d", "monthly", "5h")
    for name in rank:
        for window in pool:
            if window.name == name or (name == "weekly" and "week" in window.name):
                if name == "monthly" and window.name.startswith("monthly "):
                    continue
                return window
    return pool[0] if pool else None


def _bars(cal: float, used: float) -> list[str]:
    return [
        f"{'calendar':<8} {bar(cal)} {fmt_pct(cal)}",
        f"{'used':<8} {bar(used)} {fmt_pct(used)}",
    ]


def render_grok(quota: Quota, prev: Quota | None, now: float, tz) -> str:
    windows = _real(quota, now)
    if not windows:
        return _join([quota.title or "Grok", quota.note or "no vendor number"])
    window = windows[0]
    tags = pace_tags(window, _prev_window(prev, window),
                     now=now, prev_now=prev.checked_at if prev else None)
    if window.source == ESTIMATED:
        title = _display_title(quota, "Grok", windows)
    else:
        title = quota.title or "official weekly pool"
    lines = [_join([title, *tags])]
    cal = calendar_pct(window, now)
    used = window.used_percent
    bits = [f"used {fmt_pct(used)}", f"left {fmt_pct(100 - used)}"]
    if cal is not None:
        bits.append(f"calendar {fmt_pct(cal)}")
    if window.resets_at:
        bits.append(f"resets in {fmt_left(window.resets_at - now)}")
    lines.append(" · ".join(bits))
    if cal is not None:
        lines.extend(_bars(cal, used))
    footer = []
    if window.resets_at:
        footer.append(f"resets {_when(window.resets_at, tz)} {_tz_label(tz)}")
    for product in quota.products:
        pct = product.get("percent")
        name = product.get("name")
        if name and isinstance(pct, (int, float)) and pct > 0:
            footer.append(f"{name} {fmt_pct(pct)}")
    if footer:
        lines.append(" · ".join(footer))
    if (quota.note and windows
            and all(item.source == ESTIMATED for item in windows)):
        lines.append(quota.note)
    return "\n".join(lines)


def render_cursor(quota: Quota, prev: Quota | None, now: float, tz) -> str:
    windows = _real(quota, now)
    if not windows:
        return _join([quota.title or "Cursor", quota.note or "no vendor number"])
    extras = quota.extras
    used = extras.get("included_used_usd")
    limit = extras.get("included_limit_usd")
    remaining = extras.get("included_remaining_usd")
    monthly = next((w for w in quota.windows if w.name == "monthly"), None)
    prev_monthly = _prev_window(prev, monthly) if monthly else None
    header = _display_title(quota, "Cursor", windows)
    if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit:
        pct = used / limit * 100.0
        header = (f"{header} · included {fmt_usd(used)} / {fmt_usd(limit)}"
                  f" ({fmt_pct(pct)})")
        if isinstance(remaining, (int, float)):
            header += f" · left {fmt_usd(remaining)}"
        tags = pace_tags(monthly, prev_monthly, now=now,
                         prev_now=prev.checked_at if prev else None,
                         money=True) if monthly else []
        money = [t for t in tags if t.startswith("↑$")]
        if money:
            header += " · " + money[0]
    lines = [header]
    for window in windows:
        if window.name == "monthly":
            continue
        cal = calendar_pct(window, now)
        tags = pace_tags(window, _prev_window(prev, window),
                         now=now, prev_now=prev.checked_at if prev else None)
        if cal is None:
            lines.append(_join([f"{label_of(window)} {fmt_pct(window.used_percent)}", *tags]))
            continue
        lines.append(_join([
            f"{label_of(window)} {fmt_pct(window.used_percent)} vs calendar {fmt_pct(cal)}",
            *tags,
        ]))
        lines.extend(_bars(cal, window.used_percent))
    return "\n".join(lines)


def _window_line(window: Window, prev: Quota | None, now: float, tz,
                 *, with_tags: bool) -> list[str]:
    cal = calendar_pct(window, now)
    tags = pace_tags(window, _prev_window(prev, window),
                     now=now, prev_now=prev.checked_at if prev else None)
    extra: list[str] = []
    if window.resets_at and (window.resets_at - now) < 2 * 3600:
        extra.append(fmt_left(window.resets_at - now))
    if window.requests:
        extra.append(fmt_req(window.requests))
    if window.cost_usd is not None:
        extra.append(fmt_usd(window.cost_usd))
    if with_tags:
        bits = extra
        shown = tags + bits
    else:
        shown = extra
    name = label_of(window)
    if cal is None:
        head = f"{name} {fmt_pct(window.used_percent)}"
        return [_join([head, *shown])]
    vs = f"{name} {fmt_pct(window.used_percent)} vs calendar {fmt_pct(cal)}"
    if window.resets_at and not with_tags:
        vs += f" · resets in {fmt_left(window.resets_at - now)}"
        vs += f" · resets {_when(window.resets_at, tz)}"
    line = _join([vs, *shown] if with_tags else [vs])
    return [line, *_bars(cal, window.used_percent)]


def render_windows(spec: AgentSpec, quota: Quota, prev: Quota | None,
                   now: float, tz) -> str:
    windows = _real(quota, now)
    identity = _display_title(quota, spec.label, windows)
    if not windows:
        return _join([quota.title or spec.label, quota.note or "no vendor number"])
    fresh = [w for w in windows if just_reset(w, now)]
    live = [w for w in windows if w not in fresh]
    primary = _primary(live, now)
    if primary:
        tags = pace_tags(primary, _prev_window(prev, primary),
                         now=now, prev_now=prev.checked_at if prev else None)
        title = f"{identity}  ·  {label_of(primary)}"
        lag = [t for t in tags if t in (BEHIND, AHEAD)]
        rest = [t for t in tags if t not in (BEHIND, AHEAD)]
        if lag:
            title += " " + lag[0]
        if rest:
            title += "  ·  " + " · ".join(rest)
    else:
        title = identity
    lines = [title]
    ordered = []
    if primary:
        ordered.append(primary)
    for window in live:
        if window is not primary:
            ordered.append(window)
    for i, window in enumerate(ordered):
        lines.extend(_window_line(window, prev, now, tz, with_tags=(i > 0)))
    foot: list[str] = []
    if primary and (primary.requests or primary.cost_usd is not None):
        bits: list[str] = []
        if primary.requests:
            bits.append(fmt_req(primary.requests))
        if primary.cost_usd is not None:
            bits.append(fmt_usd(primary.cost_usd))
        prev_w = _prev_window(prev, primary)
        if (prev_w and primary.cost_usd is not None and prev_w.cost_usd is not None
                and primary.cost_usd - prev_w.cost_usd >= 0.005):
            bits.append("↑" + fmt_usd(primary.cost_usd - prev_w.cost_usd))
        foot.append(label_of(primary) + " " + " · ".join(bits) if bits
                    else label_of(primary))
    for window in fresh:
        foot.append(f"{label_of(window)} {fmt_pct(window.used_percent)} just reset")
    if foot:
        lines.append("  ·  ".join(foot) if len(foot) > 1 else foot[0])
    if (quota.note and windows
            and all(item.source == ESTIMATED for item in windows)):
        lines.append(quota.note)
    return "\n".join(lines)


def render_agent(spec: AgentSpec, quota: Quota, prev: Quota | None,
                 now: float, tz) -> str:
    if spec.key == "grok":
        return render_grok(quota, prev, now, tz)
    if spec.key == "cursor":
        return render_cursor(quota, prev, now, tz)
    return render_windows(spec, quota, prev, now, tz)


def payload(entries: list[tuple[AgentSpec, Quota, Quota | None]], *,
            now: float, strategy: str = "", order: list[str] | None = None,
            tz_name: str = "") -> dict[str, Any]:
    """Same windows `/quota` prints, as a scriptable dict. No tokens."""
    tz = _tz(tz_name)
    agents = []
    for spec, quota, _prev in entries:
        agents.append({
            "key": spec.key,
            "label": spec.label,
            "source": quota.best_source,
            "note": quota.note,
            "title": quota.title,
            "checked_at": quota.checked_at,
            "windows": [window.to_dict() for window in quota.windows],
        })
    return {
        "checked_at": now,
        "timezone": _tz_label(tz),
        "strategy": strategy,
        "order": list(order or []),
        "agents": agents,
    }


def render(entries: list[tuple[AgentSpec, Quota, Quota | None]], *,
           now: float, strategy: str = "", order: list[str] | None = None,
           tz_name: str = "") -> str:
    tz = _tz(tz_name)
    stamp = _when(now, tz, with_year=True) + " " + _tz_label(tz)
    lines = [
        f"usage rhythm  ·  {stamp}",
        f"{BEHIND} / {AHEAD} = vs calendar  ·  ↑ same-window increase  ·  "
        "new window from 0  ·  widening/narrowing compares one window",
        "",
    ]
    for spec, quota, prev in entries:
        lines.append(render_agent(spec, quota, prev, now, tz))
        lines.append("")
    if strategy:
        tail = f"strategy: {strategy}"
        if order:
            tail += f"  order: {', '.join(order)}"
        lines.append(tail)
    return "\n".join(lines).rstrip() + "\n"
