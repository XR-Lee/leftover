"""Where the quota numbers come from.

Three grades of signal, in descending order of trust:

1. reported   - the vendor already published a remaining-quota number.
                Codex: Sub2API admin `/accounts/:id/usage` when configured,
                else session logs. Claude: `GET /api/oauth/usage`.
                Grok: CLI-proxy `/v1/billing`. Cursor: `GetCurrentPeriodUsage`.
                Grok ACP `x.ai/billing` is used only when a live session exists.
2. observed   - the CLI refused us and said why.  "You've hit your weekly
                limit - resets Mon 12:00am" is a hard fact plus a reset time.
3. estimated  - nobody told us anything, so we count our own turns against a
                budget you declared in the config.

Every window carries its source so the router - and `/quota` - can be honest
about which of the three it is looking at.

These probes reuse the official CLI's own login, or a Sub2API admin key
the operator already uses to read the same Codex windows. They do not send
completions through us, and they do not hit grok.com's private gRPC-web
billing RPC.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

REPORTED, OBSERVED, ESTIMATED = "reported", "observed", "estimated"
GROK_ACP_PROBE_TIMEOUT = 3.0
LEDGER_LOCK_TIMEOUT = 5.0


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------

def _opt_float(blob: dict[str, Any], key: str) -> float | None:
    val = blob.get(key)
    if isinstance(val, bool) or val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _opt_int(blob: dict[str, Any], key: str) -> int | None:
    val = _opt_float(blob, key)
    if val is None:
        return None
    return int(val)


@dataclass
class Window:
    """One rate-limit bucket for one agent."""
    name: str                       # "5h", "weekly", "monthly", "budget"
    used_percent: float             # 0..100
    resets_at: float | None = None  # epoch seconds
    source: str = ESTIMATED
    detail: str = ""
    started_at: float | None = None
    requests: int | None = None
    cost_usd: float | None = None
    used_usd: float | None = None
    limit_usd: float | None = None

    @property
    def headroom(self) -> float:
        return max(0.0, 1.0 - self.used_percent / 100.0)

    @property
    def expired(self) -> bool:
        return self.resets_at is not None and self.resets_at <= time.time()

    def describe(self) -> str:
        left = ""
        if self.resets_at and not self.expired:
            mins = (self.resets_at - time.time()) / 60
            left = f", resets in {mins / 60:.1f}h" if mins > 90 else f", resets in {mins:.0f}m"
        return f"{self.name} {self.used_percent:.0f}% used ({self.source}{left})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "used_percent": self.used_percent,
            "resets_at": self.resets_at,
            "source": self.source,
            "detail": self.detail,
            "started_at": self.started_at,
            "requests": self.requests,
            "cost_usd": self.cost_usd,
            "used_usd": self.used_usd,
            "limit_usd": self.limit_usd,
        }

    @classmethod
    def from_dict(cls, blob: Any) -> "Window | None":
        if not isinstance(blob, dict) or not blob.get("name"):
            return None
        resets = blob.get("resets_at")
        try:
            resets_at = float(resets) if resets is not None else None
        except (TypeError, ValueError):
            resets_at = None
        try:
            used = float(blob.get("used_percent") or 0)
        except (TypeError, ValueError):
            used = 0.0
        return cls(
            name=str(blob["name"]),
            used_percent=used,
            resets_at=resets_at,
            source=str(blob.get("source") or ESTIMATED),
            detail=str(blob.get("detail") or ""),
            started_at=_opt_float(blob, "started_at"),
            requests=_opt_int(blob, "requests"),
            cost_usd=_opt_float(blob, "cost_usd"),
            used_usd=_opt_float(blob, "used_usd"),
            limit_usd=_opt_float(blob, "limit_usd"),
        )


@dataclass
class Quota:
    agent: str
    windows: list[Window] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)
    note: str = ""
    title: str = ""
    products: list[dict[str, Any]] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def headroom(self) -> float:
        """Worst-case remaining fraction across all live windows."""
        live = [w for w in self.windows if not w.expired]
        return min((w.headroom for w in live), default=1.0)

    @property
    def best_source(self) -> str:
        for grade in (REPORTED, OBSERVED, ESTIMATED):
            if any(w.source == grade for w in self.windows):
                return grade
        return ESTIMATED

    def describe(self) -> str:
        if not self.windows:
            return "no signal"
        return "; ".join(w.describe() for w in self.windows if not w.expired) or "clear"

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "checked_at": self.checked_at,
            "note": self.note,
            "title": self.title,
            "products": list(self.products),
            "extras": dict(self.extras),
            "windows": [w.to_dict() for w in self.windows],
        }

    @classmethod
    def from_dict(cls, blob: Any) -> "Quota | None":
        if not isinstance(blob, dict):
            return None
        windows: list[Window] = []
        for item in blob.get("windows") or []:
            window = Window.from_dict(item)
            if window is not None:
                windows.append(window)
        try:
            checked = float(blob.get("checked_at") or 0)
        except (TypeError, ValueError):
            checked = 0.0
        products = blob.get("products") if isinstance(blob.get("products"), list) else []
        extras = blob.get("extras") if isinstance(blob.get("extras"), dict) else {}
        return cls(
            agent=str(blob.get("agent") or ""),
            windows=windows,
            checked_at=checked,
            note=str(blob.get("note") or ""),
            title=str(blob.get("title") or ""),
            products=list(products),
            extras=dict(extras),
        )


# --------------------------------------------------------------------------
# failure classification  (grade 2: observed)
# --------------------------------------------------------------------------

@dataclass
class Failure:
    kind: str                       # "quota" | "rate_limit" | "auth" | "transient"
    resets_at: float | None = None
    window: str = ""
    detail: str = ""

    @property
    def is_hard(self) -> bool:
        """Hard failures mean 'do not retry this agent until reset'."""
        return self.kind in ("quota", "auth")


_CLOCK = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
_WEEKDAY = re.compile(
    r"resets?\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
_ISO = re.compile(r"resets?\s+(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?:\s*UTC)?", re.I)
_IN_SECONDS = re.compile(r"(?:try again|retry|available)\s+in\s+(\d+)\s*(second|minute|hour)", re.I)

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# Ordered: the first pattern that matches wins.
_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("quota", "weekly", re.compile(r"hit your weekly limit|weekly limit reached", re.I)),
    ("quota", "5h", re.compile(r"hit your session limit|session limit reached", re.I)),
    ("quota", "model", re.compile(r"hit your (opus|sonnet|haiku|fable)[\w\s]* limit", re.I)),
    ("quota", "spend", re.compile(r"spend limit reached|credit balance is too low", re.I)),
    ("quota", "plan", re.compile(
        r"usage limit|quota (?:exceeded|reached)|out of (?:credits|requests)|"
        r"message limit reached|monthly limit", re.I)),
    ("auth", "", re.compile(
        r"not (?:logged in|authenticated)|please (?:log ?in|sign ?in)|"
        r"unauthorized|401|invalid (?:api key|credentials)|authenticate", re.I)),
    ("rate_limit", "", re.compile(
        r"\b429\b|rate ?limit|too many requests|temporarily limiting requests|"
        r"overloaded|529", re.I)),
    ("transient", "", re.compile(
        r"\b5\d\d\b|timed out|timeout|connection (?:reset|closed|refused)|"
        r"econnreset|network error|stream (?:closed|ended)", re.I)),
]


def _next_clock(hour: int, minute: int, ampm: str, weekday: int | None = None) -> float:
    now = datetime.now().astimezone()
    hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is None:
        if target <= now:
            target += timedelta(days=1)
    else:
        ahead = (weekday - target.weekday()) % 7
        target += timedelta(days=ahead)
        if target <= now:
            target += timedelta(days=7)
    return target.timestamp()


def parse_reset(text: str) -> float | None:
    """Pull a reset moment out of a CLI's refusal message."""
    if m := _WEEKDAY.search(text):
        day, hh, mm, ap = m.groups()
        return _next_clock(int(hh), int(mm or 0), ap, _DAYS[day.lower()[:3]])
    if m := _ISO.search(text):
        date, hh, mm = m.groups()
        try:
            dt = datetime.fromisoformat(f"{date}T{hh}:{mm}:00+00:00")
            return dt.timestamp()
        except ValueError:
            return None
    if m := _CLOCK.search(text):
        hh, mm, ap = m.groups()
        return _next_clock(int(hh), int(mm or 0), ap)
    if m := _IN_SECONDS.search(text):
        n, unit = int(m.group(1)), m.group(2).lower()
        mult = {"second": 1, "minute": 60, "hour": 3600}[unit]
        return time.time() + n * mult
    return None


def classify(text: str | None) -> Failure | None:
    """Turn an error string into something the router can act on."""
    if not text:
        return None
    for kind, window, pattern in _RULES:
        if pattern.search(text):
            return Failure(kind=kind, window=window, resets_at=parse_reset(text),
                           detail=text.strip()[:200])
    return None


# --------------------------------------------------------------------------
# local log probes  (grade 1: reported)
# --------------------------------------------------------------------------

def _iter_recent_jsonl(root: Path, glob: str, days: int = 7,
                       max_files: int = 40) -> Iterable[Path]:
    if not root.is_dir():
        return []
    cutoff = time.time() - days * 86400
    files = [p for p in root.rglob(glob)
             if p.is_file() and p.stat().st_mtime >= cutoff]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:max_files]


def _tail_lines(path: Path, limit: int = 400) -> list[str]:
    try:
        with path.open("rb") as fh:
            return fh.read().decode(errors="replace").splitlines()[-limit:]
    except OSError:
        return []


# --------------------------------------------------------------------------
# shared helpers for vendor HTTP probes
# --------------------------------------------------------------------------

def _http_json(method: str, url: str, *, headers: dict[str, str] | None = None,
               body: Any = None, timeout: float = 8.0) -> tuple[int, Any]:
    """GET/POST JSON. Returns (status, payload). status 0 means transport error."""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        payload: Any = None
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
        return exc.code, payload
    except Exception:                              # noqa: BLE001 - probe must not raise
        return 0, None


def _parse_ts(value: Any) -> float | None:
    """Unix seconds from ISO-8601, epoch seconds, or epoch milliseconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return ts if ts > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() or (text.replace(".", "", 1).isdigit() and text.count(".") < 2):
            try:
                return _parse_ts(float(text))
            except ValueError:
                return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _box_val(value: Any) -> float | None:
    if isinstance(value, dict) and "val" in value:
        value = value.get("val")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _num(obj: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in obj:
            val = _box_val(obj.get(key))
            if val is not None:
                return val
    return None


def _keychain_password(service: str, account: str | None = None) -> str | None:
    cmd = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        cmd = ["security", "find-generic-password", "-a", account, "-s", service, "-w"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    return text or None


def _keychain_account(service: str) -> str | None:
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        if '"acct"' in line and "=" in line:
            return line.split("=", 1)[1].strip().strip('"')
    return None


def _keychain_set_password(service: str, account: str, password: str) -> bool:
    try:
        proc = subprocess.run(
            ["security", "add-generic-password", "-U",
             "-s", service, "-a", account, "-w", password],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _usable_codex_limits(limits: Any) -> bool:
    """Today's Codex often writes rate_limits as a shell of nulls. Ignore those."""
    if not isinstance(limits, dict):
        return False
    for bucket in limits.values():
        if isinstance(bucket, dict) and bucket.get("used_percent") is not None:
            return True
    return False


def _last_rate_limits(path: Path) -> dict[str, Any] | None:
    """Newest usable `rate_limits` from one Codex rollout log."""
    for line in reversed(_tail_lines(path)):
        if "rate_limits" not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = rec.get("payload", rec)
        if isinstance(payload, dict) and payload.get("type") not in (
                None, "token_count"):
            continue
        limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if _usable_codex_limits(limits):
            return limits
    return None


def probe_codex(home: Path | None = None, sub2api: Any = None,
                timeout: float | None = None) -> Quota | None:
    """Codex 5h/7d: Sub2API admin usage when configured, else session logs.

    `codex exec` may leave `rate_limits` null (openai/codex#14728), so the
    log fallback takes the newest non-null reading from *any* session.
    """
    if sub2api is not None and getattr(sub2api, "configured", False):
        found = probe_sub2api(sub2api, agent="gpt", timeout=timeout)
        if found is not None and found.windows:
            return found
        logs = _probe_codex_logs(home)
        return logs or found
    return _probe_codex_logs(home)


def _probe_codex_logs(home: Path | None = None) -> Quota | None:
    """Newest usable `rate_limits` from ~/.codex/sessions/**.jsonl."""
    root = (home or Path.home() / ".codex") / "sessions"
    saw_logs = root.is_dir()
    for path in _iter_recent_jsonl(root, "rollout-*.jsonl", max_files=80):
        limits = _last_rate_limits(path)
        if limits:
            newest = (path.stat().st_mtime, limits)
            break
    else:
        if saw_logs:
            return Quota(
                agent="gpt", windows=[],
                note="Codex logs present, but used_percent is null "
                     "(vendor often omits the number until a TUI turn)")
        return None

    observed_at, limits = newest
    windows: list[Window] = []
    for key, label in (("primary", "5h"), ("secondary", "weekly")):
        bucket = limits.get(key)
        if not isinstance(bucket, dict) or bucket.get("used_percent") is None:
            continue
        mins = bucket.get("window_minutes")
        name = label
        if isinstance(mins, (int, float)):
            name = f"{mins / 60:.0f}h" if mins < 1440 else f"{mins / 1440:.0f}d"
        resets_in = bucket.get("resets_in_seconds")
        windows.append(Window(
            name=name,
            used_percent=float(bucket["used_percent"]),
            resets_at=observed_at + float(resets_in) if resets_in is not None else None,
            source=REPORTED,
            detail="codex session log",
        ))
    if not windows:
        return None
    age = (time.time() - observed_at) / 60
    return Quota(agent="gpt", windows=windows, checked_at=observed_at,
                 note=f"from codex session log, {age:.0f}m old")


# --------------------------------------------------------------------------
# Sub2API admin  (Codex 5h/7d that session logs omit)
# --------------------------------------------------------------------------

_SUB2API_WINDOW_SECONDS = {"5h": 5 * 3600, "weekly": 7 * 86400, "7d": 7 * 86400}


def _sub2api_root(url: str) -> str:
    root = url.strip().rstrip("/")
    if root.endswith("/api/v1"):
        root = root[:-7]
    elif root.endswith("/v1"):
        root = root[:-3]
    return root.rstrip("/")


def _sub2api_data(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if code not in (None, 0, 200, "0", "success"):
        return None
    if "data" in payload:
        return payload.get("data")
    return payload


def _sub2api_headers(admin_key: str) -> dict[str, str]:
    return {"x-api-key": admin_key, "Accept": "application/json"}


def pick_sub2api_account(items: list[Any], pin: str = "") -> dict[str, Any] | None:
    """Resolve the GPT/Codex upstream account from an admin list."""
    accounts = [a for a in items if isinstance(a, dict)]
    if pin:
        needle = pin.strip().lower()
        for account in accounts:
            if str(account.get("id") or "") == pin.strip():
                return account
            name = str(account.get("name") or "").strip().lower()
            if name == needle:
                return account
        for account in accounts:
            name = str(account.get("name") or "").strip().lower()
            if needle and needle in name:
                return account
        return None

    ranked: list[tuple[int, dict[str, Any]]] = []
    for account in accounts:
        if str(account.get("status") or "active").lower() not in ("", "active"):
            continue
        platform = str(account.get("platform") or "").lower()
        if platform not in ("openai", "codex", "chatgpt"):
            continue
        typ = str(account.get("type") or account.get("account_type") or "").lower()
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        has_codex = any(str(k).startswith("codex_") for k in extra)
        score = 0
        if has_codex:
            score += 4
        if typ == "oauth":
            score += 2
        if typ != "apikey":
            score += 1
        ranked.append((score, account))
    if not ranked:
        return None
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return ranked[0][1]


def _sub2api_window(name: str, used: float, *, resets_at: float | None,
                    remaining: float | None, detail: str) -> Window:
    now = time.time()
    length = _SUB2API_WINDOW_SECONDS.get(name)
    started_at = None
    if remaining is not None and remaining > 0:
        resets_at = now + remaining
        if length:
            started_at = resets_at - length
    elif remaining is not None and remaining <= 0:
        # remaining 0 is last-reset, not next-reset (Codex 5h just flipped)
        if used <= 0 and length:
            started_at = now
            resets_at = now + length
        elif resets_at is None or resets_at <= now:
            resets_at = None
        elif length:
            started_at = resets_at - length
    elif resets_at is not None and resets_at <= now:
        if used <= 0 and length:
            started_at = now
            resets_at = now + length
        else:
            resets_at = None
    elif resets_at is not None and length:
        started_at = resets_at - length
    return Window(
        name=name,
        used_percent=min(100.0, max(0.0, used)),
        resets_at=resets_at,
        started_at=started_at,
        source=REPORTED,
        detail=detail,
    )


def parse_sub2api_usage(payload: Any, *, account_name: str = "",
                        agent: str = "gpt") -> Quota | None:
    data = _sub2api_data(payload)
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    detail = f"sub2api {account_name}".strip() if account_name else "sub2api"
    windows: list[Window] = []
    for key, name in (("five_hour", "5h"), ("seven_day", "weekly")):
        bucket = data.get(key)
        if not isinstance(bucket, dict):
            continue
        pct = bucket.get("utilization")
        if pct is None:
            pct = bucket.get("used_percent") or bucket.get("used_percentage")
        if not isinstance(pct, (int, float)):
            continue
        remaining = bucket.get("remaining_seconds")
        rem = float(remaining) if isinstance(remaining, (int, float)) else None
        window = _sub2api_window(
            name, float(pct),
            resets_at=_parse_ts(bucket.get("resets_at")),
            remaining=rem, detail=detail)
        stats = bucket.get("window_stats") if isinstance(bucket.get("window_stats"), dict) else {}
        req = stats.get("requests")
        if isinstance(req, (int, float)):
            window.requests = int(req)
        cost = stats.get("cost")
        if cost is None:
            cost = stats.get("user_cost")
        if isinstance(cost, (int, float)):
            window.cost_usd = float(cost)
        windows.append(window)
    if not windows:
        return None
    note = detail
    if account_name:
        note = f"sub2api {account_name} · admin /accounts/:id/usage"
    return Quota(agent=agent, windows=windows, note=note)


def parse_sub2api_account(account: dict[str, Any], *,
                         agent: str = "gpt") -> Quota | None:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    name = str(account.get("name") or account.get("id") or "")
    detail = f"sub2api {name} extra".strip()
    windows: list[Window] = []
    for used_key, reset_key, remain_key, label in (
            ("codex_5h_used_percent", "codex_5h_reset_at",
             "codex_5h_reset_after_seconds", "5h"),
            ("codex_7d_used_percent", "codex_7d_reset_at",
             "codex_7d_reset_after_seconds", "weekly")):
        pct = extra.get(used_key)
        if not isinstance(pct, (int, float)):
            continue
        remaining = extra.get(remain_key)
        rem = float(remaining) if isinstance(remaining, (int, float)) else None
        windows.append(_sub2api_window(
            label, float(pct),
            resets_at=_parse_ts(extra.get(reset_key)),
            remaining=rem, detail=detail))
    if not windows:
        return None
    return Quota(
        agent=agent, windows=windows,
        note=f"sub2api {name} · account extra".strip())


def _sub2api_get(root: str, admin_key: str, path: str,
                 timeout: float = 12.0) -> tuple[int, Any]:
    return _http_json(
        "GET", f"{root}{path}",
        headers=_sub2api_headers(admin_key), timeout=timeout)


def _probe_request_timeout(deadline: float | None, cap: float) -> float | None:
    if deadline is None:
        return cap
    remaining = deadline - time.monotonic()
    return min(cap, remaining) if remaining > 0 else None


def _sub2api_list_accounts(root: str, admin_key: str,
                           deadline: float | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while page <= 20:
        request_timeout = _probe_request_timeout(deadline, 12.0)
        if request_timeout is None:
            break
        status, payload = _sub2api_get(
            root, admin_key,
            f"/api/v1/admin/accounts?page={page}&page_size=50",
            timeout=request_timeout)
        data = _sub2api_data(payload)
        if status != 200 or not isinstance(data, dict):
            break
        batch = data.get("items") or []
        items.extend(a for a in batch if isinstance(a, dict))
        try:
            pages = int(data.get("pages") or 1)
        except (TypeError, ValueError):
            pages = 1
        if page >= pages or not batch:
            break
        page += 1
    return items


def probe_sub2api(sub2api: Any, *, agent: str = "gpt",
                  timeout: float | None = None) -> Quota | None:
    """Read Codex 5h/7d from Sub2API admin. No completions, admin GET only."""
    base = str(getattr(sub2api, "base_url", "") or "").strip()
    key = str(getattr(sub2api, "admin_key", "") or "").strip()
    pin = str(getattr(sub2api, "gpt_account", "") or "").strip()
    if not base or not key:
        return None
    deadline = (None if timeout is None else
                time.monotonic() + max(0.0, timeout))
    root = _sub2api_root(base)
    items = _sub2api_list_accounts(root, key, deadline)
    if not items:
        return Quota(agent=agent, windows=[],
                     note="sub2api accounts list failed")
    account = pick_sub2api_account(items, pin)
    if account is None:
        hint = f" matching {pin}" if pin else " (openai oauth / Codex extra)"
        return Quota(agent=agent, windows=[],
                     note=f"no sub2api GPT account{hint}")
    account_id = account.get("id")
    account_name = str(account.get("name") or account_id or "")
    extra_blob = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    title = str(extra_blob.get("email") or account_name)

    def _named(quota: Quota | None) -> Quota | None:
        if quota is not None and not quota.title:
            quota.title = title
        return quota

    if account_id is not None:
        request_timeout = _probe_request_timeout(deadline, 12.0)
        if request_timeout is not None:
            status, payload = _sub2api_get(
                root, key,
                f"/api/v1/admin/accounts/{account_id}/usage?source=active",
                timeout=request_timeout)
            parsed = parse_sub2api_usage(
                payload, account_name=account_name, agent=agent)
            if status == 200 and parsed is not None:
                return _named(parsed)
        request_timeout = _probe_request_timeout(deadline, 12.0)
        if request_timeout is not None:
            status, payload = _sub2api_get(
                root, key, f"/api/v1/admin/accounts/{account_id}",
                timeout=request_timeout)
            data = _sub2api_data(payload)
            if status == 200 and isinstance(data, dict):
                extra = parse_sub2api_account(data, agent=agent)
                if extra is not None:
                    return _named(extra)
    extra = parse_sub2api_account(account, agent=agent)
    if extra is not None:
        return _named(extra)
    return Quota(
        agent=agent, windows=[],
        note=f"sub2api {account_name} has no Codex 5h/7d")


def probe_grok_local(home: Path | None = None) -> Quota | None:
    """Best-effort read of ~/.grok/sessions/*/signals.json token counters.

    Grok Build does not publish a percentage locally, so this only surfaces
    activity - the router treats it as context, not as a limit.
    """
    root = (home or Path.home() / ".grok") / "sessions"
    if not root.is_dir():
        return None
    total = 0
    for path in sorted(root.glob("*/signals.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for key in ("totalTokensBeforeCompaction", "contextTokensUsed"):
            val = data.get(key)
            if isinstance(val, (int, float)):
                total += int(val)
                break
    if not total:
        return None
    return Quota(agent="grok", windows=[],
                 note=f"session tokens ~{total // 1000}k (not the plan cap)")


def _consume_background_task(task: asyncio.Future[Any]) -> None:
    """Retrieve a detached task result so late failures stay quiet."""
    try:
        task.exception()
    except BaseException:  # cancellation is expected after a hard timeout
        pass


async def _hard_timeout(awaitable: Any, timeout: float) -> Any:
    """Return at the deadline even if cancellation cleanup is uncooperative."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout))
    except BaseException:
        task.cancel()
        task.add_done_callback(_consume_background_task)
        raise
    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_background_task)
        raise TimeoutError
    return task.result()


async def probe_grok_acp(
    conn: Any, timeout: float = GROK_ACP_PROBE_TIMEOUT,
) -> Quota | None:
    """Ask Grok Build for its billing period over the ACP extension method."""
    if conn is None:
        return None
    try:
        raw = await _hard_timeout(
            conn.ext_method("x.ai/billing", {}), timeout)
    except Exception:                                # noqa: BLE001 - optional extension
        return None
    if not isinstance(raw, dict):
        return None
    limit = (raw.get("monthlyLimit") or {}).get("val")
    used = ((raw.get("usage") or {}).get("totalUsed") or {}).get("val")
    if not limit or used is None:
        return None
    end = (raw.get("billingCycle") or {}).get("billingPeriodEnd")
    resets_at = None
    if isinstance(end, str):
        try:
            resets_at = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
        except ValueError:
            resets_at = None
    return Quota(agent="grok", windows=[Window(
        name="monthly",
        used_percent=min(100.0, float(used) / float(limit) * 100.0),
        resets_at=resets_at,
        source=REPORTED,
        detail="x.ai/billing",
    )])


def _grok_auth_entry(home: Path | None = None) -> dict[str, Any] | None:
    """Pick the SuperGrok OIDC session from ~/.grok/auth.json. Skip API keys."""
    root = home or (Path.home() / ".grok")
    path = root / "auth.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    entries: list[tuple[int, dict[str, Any]]] = []
    for key, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        mode = str(rec.get("auth_mode") or rec.get("authMode") or "").lower()
        if mode in ("api_key", "apikey", "web_login"):
            continue
        if str(key).startswith("xai::"):
            continue
        token = rec.get("key") or rec.get("access_token")
        if not isinstance(token, str) or not token:
            continue
        exp = _parse_ts(rec.get("expires_at") or rec.get("expiresAt"))
        if exp is not None and exp <= time.time() + 30:
            continue
        rank = 0 if str(key).startswith("https://auth.x.ai") else 1
        entries.append((rank, rec))
    if not entries:
        return None
    entries.sort(key=lambda pair: pair[0])
    return entries[0][1]


def _grok_period_name(cfg: dict[str, Any]) -> str:
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    kind = str(period.get("type") or "")
    if "WEEKLY" in kind.upper():
        return "weekly"
    if "MONTHLY" in kind.upper():
        return "monthly"
    start = _parse_ts(period.get("start") or cfg.get("billingPeriodStart")
                      or ((cfg.get("billingCycle") or {}).get("billingPeriodStart")
                          if isinstance(cfg.get("billingCycle"), dict) else None))
    end = _parse_ts(period.get("end") or cfg.get("billingPeriodEnd")
                    or ((cfg.get("billingCycle") or {}).get("billingPeriodEnd")
                        if isinstance(cfg.get("billingCycle"), dict) else None))
    if start and end and end > start:
        days = (end - start) / 86400
        if 4 <= days <= 12:
            return "weekly"
        if 20 <= days <= 45:
            return "monthly"
    return "credits"


def _grok_products(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    raw = cfg.get("productUsage") or cfg.get("product_usage") or []
    if not isinstance(raw, list):
        return []
    labels = {
        "GrokBuild": "Build", "GrokChat": "Chat", "GrokImagine": "Imagine",
        "GrokVoice": "Voice", "GrokAppBuilder": "AppBuilder", "GrokTasks": "Tasks",
    }
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("product") or item.get("name")
        pct = item.get("usagePercent")
        if pct is None:
            pct = item.get("usage_percent")
        if not name or not isinstance(pct, (int, float)) or pct <= 0:
            continue
        label = labels.get(str(name), str(name).removeprefix("Grok"))
        out.append({"name": label, "percent": float(pct)})
    return out


def parse_grok_billing(payload: dict[str, Any], *, plan: str = "") -> Quota | None:
    """ACP `x.ai/billing` and CLI-proxy `/v1/billing` both collapse to one window."""
    cfg = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    if not isinstance(cfg, dict):
        return None
    pct = cfg.get("creditUsagePercent")
    if not isinstance(pct, (int, float)):
        used = _num(cfg, "used")
        if used is None:
            usage = cfg.get("usage") if isinstance(cfg.get("usage"), dict) else {}
            used = _num(usage, "totalUsed") if usage else None
        limit = _num(cfg, "monthlyLimit")
        if used is None or not limit:
            return None
        pct = used / limit * 100.0
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    cycle = cfg.get("billingCycle") if isinstance(cfg.get("billingCycle"), dict) else {}
    started_at = _parse_ts(
        period.get("start") or cfg.get("billingPeriodStart")
        or cycle.get("billingPeriodStart"))
    resets_at = _parse_ts(
        period.get("end") or cfg.get("billingPeriodEnd") or cycle.get("billingPeriodEnd"))
    name = _grok_period_name(cfg)
    note = "grok cli-proxy /v1/billing"
    if plan:
        note = f"{plan} · {note}"
    title = f"官方周池 · {plan}" if plan else "官方周池"
    return Quota(
        agent="grok",
        windows=[Window(
            name=name,
            used_percent=min(100.0, max(0.0, float(pct))),
            resets_at=resets_at,
            started_at=started_at,
            source=REPORTED,
            detail="x.ai billing",
        )],
        note=note,
        title=title,
        products=_grok_products(cfg),
        extras={"plan": plan},
    )


def probe_grok_rest(home: Path | None = None,
                    timeout: float | None = None) -> Quota | None:
    """Official Grok CLI billing REST, using the local OIDC session.

    Faster and more reliable than spawning `grok agent stdio` just to call
    `x.ai/billing` (that method is often missing on the stdio surface).
    Does not hit grok.com gRPC-web.
    """
    root = home or (Path.home() / ".grok")
    entry = _grok_auth_entry(root)
    if entry is None:
        return None
    deadline = (None if timeout is None else
                time.monotonic() + max(0.0, timeout))
    token = entry.get("key") or entry.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    uid = str(entry.get("user_id") or entry.get("userId") or "")
    version = "1.0.5"
    ver_path = root / "version.json"
    try:
        ver = json.loads(ver_path.read_text())
        if isinstance(ver, dict) and isinstance(ver.get("version"), str):
            version = ver["version"]
    except (OSError, json.JSONDecodeError):
        pass
    headers = {
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "Accept": "application/json",
        "x-grok-client-version": version,
        "x-grok-client-mode": "interactive",
    }
    if uid:
        headers["x-userid"] = uid
    request_timeout = _probe_request_timeout(deadline, 8.0)
    if request_timeout is None:
        return None
    status, payload = _http_json(
        "GET", "https://cli-chat-proxy.grok.com/v1/billing?format=credits",
        headers=headers, timeout=request_timeout)
    if status != 200 or not isinstance(payload, dict):
        request_timeout = _probe_request_timeout(deadline, 8.0)
        if request_timeout is None:
            return None
        status, payload = _http_json(
            "GET", "https://cli-chat-proxy.grok.com/v1/billing",
            headers=headers, timeout=request_timeout)
    if status != 200 or not isinstance(payload, dict):
        return None
    plan = ""
    request_timeout = _probe_request_timeout(deadline, 3.0)
    if request_timeout is not None:
        s_status, settings = _http_json(
            "GET", "https://cli-chat-proxy.grok.com/v1/settings",
            headers=headers, timeout=request_timeout)
        if s_status == 200 and isinstance(settings, dict):
            plan = str(settings.get("subscription_tier_display")
                       or settings.get("subscriptionTier") or "")
    return parse_grok_billing(payload, plan=plan)


async def probe_grok_billing() -> Quota | None:
    """Network probe used when no live Grok ACP session exists."""
    return await asyncio.to_thread(probe_grok_rest)


# --------------------------------------------------------------------------
# Claude Code  (GET /api/oauth/usage — same endpoint as `/usage`)
# --------------------------------------------------------------------------

_CLAUDE_KEYCHAIN = "Claude Code-credentials"
_CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_CLAUDE_TOKEN_URLS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)
_CLAUDE_FLAT = (
    ("five_hour", "5h"),
    ("seven_day", "weekly"),
    ("seven_day_opus", "weekly opus"),
    ("seven_day_sonnet", "weekly sonnet"),
)


def _claude_dir(home: Path | None = None) -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return (home or Path.home() / ".claude")


def _claude_oauth_from_mapping(blob: Any) -> dict[str, Any] | None:
    if not isinstance(blob, dict):
        return None
    oauth = blob.get("claudeAiOauth") or blob.get("claudeAi")
    if isinstance(oauth, dict) and (oauth.get("accessToken") or oauth.get("access")):
        return blob
    return None


def _claude_creds(home: Path | None = None) -> tuple[dict[str, Any], str] | None:
    """Return (credentials JSON, source). source is 'env', 'file', or 'keychain'."""
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return {"claudeAiOauth": {"accessToken": env}}, "env"
    path = _claude_dir(home) / ".credentials.json"
    try:
        blob = json.loads(path.read_text())
        if _claude_oauth_from_mapping(blob):
            return blob, "file"
    except (OSError, json.JSONDecodeError):
        pass
    account = _keychain_account(_CLAUDE_KEYCHAIN) or os.environ.get("USER")
    raw = _keychain_password(_CLAUDE_KEYCHAIN, account)
    if not raw:
        raw = _keychain_password(_CLAUDE_KEYCHAIN)
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        blob = {"claudeAiOauth": {"accessToken": raw}}
    if not _claude_oauth_from_mapping(blob):
        return None
    return blob, "keychain"


def _claude_plan(blob: dict[str, Any], home: Path | None = None) -> str:
    cfg = Path.home() / ".claude.json"
    try:
        data = json.loads(cfg.read_text())
        acct = data.get("oauthAccount") if isinstance(data, dict) else None
        if isinstance(acct, dict):
            org = acct.get("organizationType")
            if isinstance(org, str) and org:
                return org.removeprefix("claude_")
    except (OSError, json.JSONDecodeError):
        pass
    oauth = blob.get("claudeAiOauth") or blob.get("claudeAi") or {}
    tier = oauth.get("rateLimitTier") or oauth.get("subscriptionType") or ""
    if isinstance(tier, str) and tier:
        return tier.removeprefix("default_claude_").removeprefix("claude_")
    return ""


def _persist_claude_creds(blob: dict[str, Any], source: str,
                          home: Path | None = None) -> None:
    text = json.dumps(blob)
    if source == "file":
        path = _claude_dir(home) / ".credentials.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    if source == "keychain":
        account = _keychain_account(_CLAUDE_KEYCHAIN) or os.environ.get("USER") or "claude"
        _keychain_set_password(_CLAUDE_KEYCHAIN, account, text)


def _refresh_claude_oauth(blob: dict[str, Any], source: str,
                          home: Path | None = None) -> dict[str, Any] | None:
    oauth = dict(blob.get("claudeAiOauth") or blob.get("claudeAi") or {})
    refresh = oauth.get("refreshToken") or oauth.get("refresh")
    if not isinstance(refresh, str) or not refresh:
        return None
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": _CLAUDE_CLIENT_ID,
    }
    scopes = oauth.get("scopes")
    if isinstance(scopes, list) and scopes:
        payload["scope"] = " ".join(str(s) for s in scopes)
    result: Any = None
    for url in _CLAUDE_TOKEN_URLS:
        status, body = _http_json("POST", url, body=payload, timeout=10.0)
        if status == 200 and isinstance(body, dict) and body.get("access_token"):
            result = body
            break
    if not isinstance(result, dict):
        return None
    oauth["accessToken"] = result["access_token"]
    if result.get("refresh_token"):
        oauth["refreshToken"] = result["refresh_token"]
    expires_in = result.get("expires_in", 3600)
    try:
        oauth["expiresAt"] = int(time.time() * 1000 + float(expires_in) * 1000)
    except (TypeError, ValueError):
        oauth["expiresAt"] = int(time.time() * 1000 + 3600_000)
    updated = dict(blob)
    if "claudeAiOauth" in blob or "claudeAi" not in blob:
        updated["claudeAiOauth"] = oauth
    else:
        updated["claudeAi"] = oauth
    _persist_claude_creds(updated, source, home)
    return updated


def _window_started(name: str, resets_at: float | None) -> float | None:
    if resets_at is None:
        return None
    key = name.lower()
    if key == "5h" or key == "session":
        return resets_at - 5 * 3600
    if "week" in key:
        return resets_at - 7 * 86400
    return None


def _claude_display_name(home: Path | None = None) -> str:
    cfg = (home or Path.home()) / ".claude.json"
    if home is not None:
        cfg = Path(home) / ".claude.json"
    try:
        data = json.loads(cfg.read_text())
        acct = data.get("oauthAccount") if isinstance(data, dict) else None
        if isinstance(acct, dict):
            name = acct.get("displayName") or acct.get("emailAddress") or ""
            if isinstance(name, str):
                return name
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def parse_claude_usage(payload: dict[str, Any], *, plan: str = "",
                       identity: str = "") -> Quota | None:
    windows: list[Window] = []
    limits = payload.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            pct = entry.get("percent")
            if pct is None:
                pct = entry.get("utilization")
            if not isinstance(pct, (int, float)):
                continue
            kind = str(entry.get("kind") or "")
            name = "5h" if kind == "session" else "weekly"
            if kind == "weekly_scoped":
                scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
                model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
                label = model.get("display_name") or model.get("id") or "model"
                name = f"weekly {label}"
            elif kind and kind not in ("session", "weekly_all"):
                name = kind.replace("_", " ")
            resets_at = _parse_ts(entry.get("resets_at"))
            windows.append(Window(
                name=name,
                used_percent=min(100.0, max(0.0, float(pct))),
                resets_at=resets_at,
                started_at=_window_started(name, resets_at),
                source=REPORTED,
                detail="anthropic oauth/usage",
            ))
    if not windows:
        for key, name in _CLAUDE_FLAT:
            bucket = payload.get(key)
            if not isinstance(bucket, dict):
                continue
            pct = bucket.get("utilization")
            if pct is None:
                pct = bucket.get("used_percentage") or bucket.get("percent")
            if not isinstance(pct, (int, float)):
                continue
            resets_at = _parse_ts(bucket.get("resets_at"))
            windows.append(Window(
                name=name,
                used_percent=min(100.0, max(0.0, float(pct))),
                resets_at=resets_at,
                started_at=_window_started(name, resets_at),
                source=REPORTED,
                detail="anthropic oauth/usage",
            ))
    extra_note = ""
    extra = payload.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used = extra.get("used_credits")
        limit = extra.get("monthly_limit")
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit:
            extra_note = f"extra ${used/100:.2f}/${limit/100:.2f}"
    if not windows:
        return None
    note = "anthropic /api/oauth/usage"
    if extra_note:
        note = f"{note} · {extra_note}"
    if plan:
        note = f"plan {plan} · {note}"
    title = "Claude"
    if identity:
        title = f"Claude · {identity}"
    elif plan:
        title = f"Claude · {plan}"
    return Quota(agent="claude", windows=windows, note=note, title=title,
                 extras={"plan": plan, "identity": identity})


def probe_claude(home: Path | None = None) -> Quota | None:
    """Same remaining-quota numbers Claude Code shows in `/usage`."""
    found = _claude_creds(home)
    if found is None:
        cache = _claude_dir(home) / "usage-limits.json"
        try:
            cached = json.loads(cache.read_text())
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict):
            rebuilt = _quota_from_ccusage_cache(cached)
            if rebuilt is not None:
                return rebuilt
        return Quota(agent="claude", windows=[],
                     note="Claude OAuth token not readable")
    blob, source = found
    oauth = blob.get("claudeAiOauth") or blob.get("claudeAi") or {}
    token = oauth.get("accessToken") or oauth.get("access")
    expires_at = oauth.get("expiresAt") or oauth.get("expires")
    exp_ms = expires_at if isinstance(expires_at, (int, float)) else None
    if exp_ms is not None and exp_ms < 1e12:
        exp_ms *= 1000
    if source != "env" and (exp_ms is None or time.time() * 1000 > exp_ms - 60_000):
        refreshed = _refresh_claude_oauth(blob, source, home)
        if refreshed is not None:
            blob = refreshed
            oauth = blob.get("claudeAiOauth") or blob.get("claudeAi") or {}
            token = oauth.get("accessToken") or oauth.get("access")
    if not isinstance(token, str) or not token:
        return Quota(agent="claude", windows=[],
                     note="Claude OAuth token missing — run `claude` to log in")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "claude-code/2.1.239",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "x-app": "cli",
    }
    status, payload = _http_json(
        "GET", "https://api.anthropic.com/api/oauth/usage", headers=headers)
    if status in (401, 403) and source != "env":
        refreshed = _refresh_claude_oauth(blob, source, home)
        if refreshed is not None:
            oauth = refreshed.get("claudeAiOauth") or refreshed.get("claudeAi") or {}
            token = oauth.get("accessToken") or oauth.get("access")
            if isinstance(token, str) and token:
                headers["Authorization"] = f"Bearer {token}"
                status, payload = _http_json(
                    "GET", "https://api.anthropic.com/api/oauth/usage",
                    headers=headers)
    if status != 200 or not isinstance(payload, dict):
        cache = _claude_dir(home) / "usage-limits.json"
        try:
            cached = json.loads(cache.read_text())
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict):
            rebuilt = _quota_from_ccusage_cache(cached)
            if rebuilt is not None:
                rebuilt.note = (rebuilt.note + " (cached)").strip()
                return rebuilt
        return Quota(agent="claude", windows=[],
                     note=f"Claude usage API HTTP {status or 'error'}")
    return parse_claude_usage(
        payload, plan=_claude_plan(blob, home),
        identity=_claude_display_name(home))


def _quota_from_ccusage_cache(data: dict[str, Any]) -> Quota | None:
    """Best-effort read of ~/.claude/usage-limits.json written by ccusage."""
    windows: list[Window] = []
    mapping = {"session": "5h", "5h": "5h", "7d": "weekly"}
    for key, val in data.items():
        if not isinstance(val, dict) or "pct" not in val:
            continue
        pct = val.get("pct")
        if not isinstance(pct, (int, float)):
            continue
        name = mapping.get(key, key.replace("7d_", "weekly ").replace("_", " "))
        windows.append(Window(
            name=name,
            used_percent=min(100.0, max(0.0, float(pct))),
            resets_at=_parse_ts(val.get("resets_at")),
            source=REPORTED,
            detail="ccusage cache",
        ))
    if not windows:
        return None
    plan = data.get("plan") or ""
    note = "ccusage cache"
    if isinstance(plan, str) and plan:
        note = f"plan {plan} · {note}"
    return Quota(agent="claude", windows=windows, note=note)


# --------------------------------------------------------------------------
# Cursor  (dashboard GetCurrentPeriodUsage via the local login token)
# --------------------------------------------------------------------------

def _cursor_cli_tier() -> str:
    import shutil
    binary = shutil.which("cursor-agent")
    if not binary:
        return ""
    try:
        proc = subprocess.run(
            [binary, "about"], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (proc.stdout or "") + (proc.stderr or "")
    for line in text.splitlines():
        if "subscription" in line.lower() or "tier" in line.lower():
            parts = line.split()
            if parts:
                return parts[-1]
    return ""


def _cursor_state_db() -> Path:
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "Cursor"
                / "User" / "globalStorage" / "state.vscdb")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _cursor_sqlite_token(db: Path | None = None) -> str | None:
    path = db or _cursor_state_db()
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                ("cursorAuth/accessToken",)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    val = row[0]
    if isinstance(val, bytes):
        val = val.decode("utf-8", errors="replace")
    if not isinstance(val, str) or not val:
        return None
    text = val.strip()
    if text.startswith('"') and text.endswith('"'):
        try:
            text = json.loads(text)
        except json.JSONDecodeError:
            text = text.strip('"')
    return text if isinstance(text, str) and text else None


def _cursor_sqlite_membership(db: Path | None = None) -> str:
    path = db or _cursor_state_db()
    if not path.is_file():
        return ""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                ("cursorAuth/stripeMembershipType",)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    if not row or not row[0]:
        return ""
    val = row[0]
    if isinstance(val, bytes):
        val = val.decode("utf-8", errors="replace")
    return str(val).strip().strip('"')


def _cursor_token() -> str | None:
    tok = _cursor_sqlite_token()
    if tok:
        return tok
    return _keychain_password("cursor-access-token", "cursor-user") \
        or _keychain_password("cursor-access-token")


def parse_cursor_usage(payload: dict[str, Any], *, tier: str = "") -> Quota | None:
    plan = payload.get("planUsage") if isinstance(payload.get("planUsage"), dict) \
        else payload
    if not isinstance(plan, dict):
        return None
    used = _num(plan, "totalSpend", "includedSpend", "used")
    limit = _num(plan, "limit", "includedSpendLimit")
    remaining = _num(plan, "remaining")
    if limit and used is None and remaining is not None:
        used = max(0.0, limit - remaining)
    if not limit or used is None:
        return None
    pct = min(100.0, max(0.0, used / limit * 100.0))
    resets_at = _parse_ts(payload.get("billingCycleEnd") or payload.get("endOfMonth")
                          or plan.get("billingCycleEnd"))
    started_at = _parse_ts(payload.get("billingCycleStart") or payload.get("startOfMonth")
                           or plan.get("billingCycleStart"))
    used_usd, limit_usd = used / 100.0, limit / 100.0
    remaining_usd = (remaining / 100.0) if remaining is not None else max(0.0, limit_usd - used_usd)
    windows = [Window(
        name="monthly",
        used_percent=pct,
        resets_at=resets_at,
        started_at=started_at,
        source=REPORTED,
        detail="cursor dashboard",
        used_usd=used_usd,
        limit_usd=limit_usd,
    )]
    for key, name in (("autoPercentUsed", "monthly auto"),
                      ("apiPercentUsed", "monthly api")):
        val = plan.get(key)
        if isinstance(val, (int, float)):
            windows.append(Window(
                name=name,
                used_percent=min(100.0, max(0.0, float(val))),
                resets_at=resets_at,
                started_at=started_at,
                source=REPORTED,
                detail="cursor dashboard",
            ))
    msg = payload.get("displayMessage")
    note = "cursor GetCurrentPeriodUsage"
    if isinstance(msg, str) and msg:
        note = msg
    if tier:
        note = f"plan {tier} · {note}"
    plan_label = (tier or "").strip()
    if plan_label.lower() == "ultra":
        title = "Cursor Ultra"
    elif plan_label:
        title = f"Cursor {plan_label}"
    else:
        title = "Cursor"
    return Quota(
        agent="cursor", windows=windows, note=note, title=title,
        extras={
            "plan": plan_label,
            "included_used_usd": used_usd,
            "included_limit_usd": limit_usd,
            "included_remaining_usd": remaining_usd,
        },
    )


def probe_cursor(db: Path | None = None) -> Quota | None:
    """Plan remaining from Cursor's dashboard API, using the local IDE login."""
    tier = _cursor_sqlite_membership(db) or _cursor_cli_tier()
    token = _cursor_sqlite_token(db) if db is not None else _cursor_token()
    found: Quota | None = None
    if token:
        status, payload = _http_json(
            "POST",
            "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
            },
            body={},
        )
        if status == 200 and isinstance(payload, dict):
            found = parse_cursor_usage(payload, tier=tier)
    if found is not None:
        return found
    note = "Cursor CLI does not publish remaining credits"
    if token:
        note = "Cursor dashboard usage unavailable"
    if tier:
        note = f"plan {tier} · {note}"
    return Quota(agent="cursor", windows=[], note=note)


# --------------------------------------------------------------------------
# local ledger  (grade 3: estimated)
# --------------------------------------------------------------------------

class Ledger:
    """Counts our own turns per agent, so agents that report nothing still
    get a headroom estimate against budgets you declare."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.events: dict[str, list[list[float]]] = {}   # key -> [[ts, seconds, ok]]
        self._load()

    @staticmethod
    def _read_events(path: Path) -> dict[str, list[list[float]]]:
        try:
            blob = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        raw = blob.get("events") if isinstance(blob, dict) else None
        if not isinstance(raw, dict):
            return {}
        events: dict[str, list[list[float]]] = {}
        for key, rows in raw.items():
            if not isinstance(key, str) or not isinstance(rows, list):
                continue
            valid: list[list[float]] = []
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 3:
                    continue
                try:
                    valid.append([
                        float(row[0]), float(row[1]), float(row[2])])
                except (TypeError, ValueError):
                    continue
            if valid:
                events[key] = valid
        return events

    def _load(self) -> None:
        self.events = self._read_events(self.path)

    @staticmethod
    def _merge_events(
        *sources: dict[str, list[list[float]]],
    ) -> dict[str, list[list[float]]]:
        """Merge stale snapshots without duplicating rows they share."""
        cutoff = time.time() - 8 * 86400
        keys = {key for source in sources for key in source}
        merged: dict[str, list[list[float]]] = {}
        for key in keys:
            union: Counter[tuple[float, float, float]] = Counter()
            for source in sources:
                rows = source.get(key, [])
                counts = Counter(
                    (float(row[0]), float(row[1]), float(row[2]))
                    for row in rows if len(row) >= 3 and float(row[0]) >= cutoff
                )
                union |= counts
            rows = [list(row) for row, count in union.items()
                    for _ in range(count)]
            rows.sort(key=lambda row: row[0])
            if rows:
                merged[key] = rows[-2000:]
        return merged

    def _acquire_lock(self, lock_file: Any) -> None:
        deadline = time.monotonic() + LEDGER_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out locking quota ledger {self.path}")
                time.sleep(0.01)

    def _atomic_write(self, events: dict[str, list[list[float]]]) -> None:
        fd, raw_tmp = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"events": events}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _locked_save(self, new_row: tuple[str, list[float]] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        with lock_path.open("a+b") as lock_file:
            self._acquire_lock(lock_file)
            try:
                events = self._merge_events(
                    self._read_events(self.path), self.events)
                if new_row is not None:
                    key, row = new_row
                    events.setdefault(key, []).append(row)
                    events[key] = events[key][-2000:]
                self._atomic_write(events)
                self.events = events
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def save(self) -> None:
        try:
            self._locked_save()
        except (OSError, TimeoutError):
            pass

    def record(self, key: str, seconds: float, ok: bool) -> None:
        row = [time.time(), round(seconds, 1), 1.0 if ok else 0.0]
        try:
            self._locked_save((key, row))
        except (OSError, TimeoutError):
            # The ledger is advisory. Keep the event in memory so a later
            # successful record can merge and persist it.
            events = self._merge_events(self.events)
            events.setdefault(key, []).append(row)
            events[key] = events[key][-2000:]
            self.events = events

    def count(self, key: str, window_seconds: float, successful_only: bool = True) -> int:
        cutoff = time.time() - window_seconds
        return sum(1 for r in self.events.get(key, [])
                   if r[0] >= cutoff and (r[2] > 0 or not successful_only))

    def budget_windows(self, key: str, per_5h: int | None,
                       per_week: int | None) -> list[Window]:
        windows: list[Window] = []
        for limit, seconds, label in ((per_5h, 5 * 3600, "5h"),
                                      (per_week, 7 * 86400, "weekly")):
            if not limit:
                continue
            used = self.count(key, seconds)
            windows.append(Window(
                name=f"{label} budget",
                used_percent=min(100.0, used / limit * 100.0),
                resets_at=time.time() + seconds,
                source=ESTIMATED,
                detail=f"{used}/{limit} turns",
            ))
        return windows
