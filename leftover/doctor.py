"""`leftover doctor` — who's in the room, remaining bars, config paths.

Shape matches usher doctor. Remaining is the latest quota snapshot
(reported/observed, else estimated local), not usher's launch-confidence
ledger.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .config import DEFAULT_CONFIG_PATHS, AgentSpec, Config
from .quota import ESTIMATED, OBSERVED, REPORTED, Quota
from .scope import doctor_line
from . import ui

INSTALL_HINTS = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "gpt": "npm install -g @openai/codex",
    "grok": "curl -fsSL https://x.ai/cli/install.sh | bash",
    "cursor": "curl https://cursor.com/install -fsS | bash",
    "antigravity": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
}


async def _probe(argv: list[str], timeout: float = 25.0) -> tuple[bool, str]:
    if not argv or shutil.which(argv[0]) is None:
        return False, f"`{argv[0] if argv else '?'}` not on PATH"
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return False, "timed out"
    except Exception as exc:                       # noqa: BLE001
        return False, str(exc)
    blob = (out + err).decode(errors="replace").strip().splitlines()
    first = blob[0][:80] if blob else ""
    return proc.returncode == 0, first or f"exit {proc.returncode}"


def _hint(spec: AgentSpec) -> str:
    return INSTALL_HINTS.get(spec.key, f"install `{spec.binary or spec.key}`")


def _remaining(quota: Quota | None) -> tuple[float | None, str, float | None]:
    if quota is None:
        return None, "", None
    live = [window for window in quota.windows if not window.expired]
    vendor = [
        window for window in live
        if window.source in (REPORTED, OBSERVED)
    ]
    extra = next((window for window in vendor if window.name == "extra"), None)
    plan = [window for window in vendor if window.name != "extra"]
    pool = plan or vendor or [
        window for window in live if window.source == ESTIMATED
    ]
    if not pool:
        return None, "", None
    worst = min(pool, key=lambda window: window.headroom)
    extra_left = None
    if extra is not None and extra is not worst:
        extra_left = extra.headroom * 100.0
    return worst.headroom * 100.0, worst.name, extra_left


def _quota_from_cache(spec: AgentSpec, cached: dict) -> Quota | None:
    blob = cached.get(spec.key)
    if not isinstance(blob, dict):
        return None
    return Quota.from_dict(blob)


async def check(spec: AgentSpec) -> str:
    """Verbose per-agent probe. Kept for legacy `agora doctor` callers."""
    lines = [f"{spec.emoji} {spec.label} ({spec.key})"]
    if not spec.enabled:
        return lines[0] + "  [disabled]"

    binaries = {c[0] for c in (spec.acp_command, spec.exec_command) if c}
    for b in sorted(binaries):
        where = shutil.which(b)
        lines.append(f"    binary {b}: {where or 'NOT FOUND'}")

    if spec.exec_command:
        ok, msg = await _probe([spec.exec_command[0], "--version"])
        lines.append(f"    exec  : {'ok' if ok else 'fail'} - {msg}")
    if spec.acp_command:
        have = shutil.which(spec.acp_command[0]) is not None
        lines.append(f"    acp   : {'command present' if have else 'command missing'}"
                     f" - {' '.join(spec.acp_command)}")
    return "\n".join(lines)


def _quota_of(spec: AgentSpec, cached: dict,
              quotas: dict[str, Quota] | None) -> Quota | None:
    if quotas and spec.key in quotas:
        found = quotas[spec.key]
        return found if isinstance(found, Quota) else Quota.from_dict(found)
    return _quota_from_cache(spec, cached)


async def _roster_line(spec: AgentSpec, cached: dict, width: int = 10,
                       quotas: dict[str, Quota] | None = None) -> str:
    name = f"{spec.label:<{width}}"
    if not spec.enabled:
        return ui.dim(f"  {name} disabled")
    if not spec.installed:
        return ui.dim(f"  {name} not installed → {_hint(spec)}")

    version = ""
    binary = spec.binary
    if binary:
        ok, msg = await _probe([binary, "--version"])
        if ok and msg:
            version = msg.split(" (")[0].strip()[:16]
    version = f"{version:<16}" if version else f"{'installed':<16}"

    remaining, window, extra_left = _remaining(_quota_of(spec, cached, quotas))
    if remaining is None:
        return f"  {name} {version} remaining —"
    bar = ui.remaining_bar(remaining)
    extra = f"  {window}" if window else ""
    if extra_left is not None:
        extra += f"  extra {extra_left:3.0f}%"
    return f"  {name} {version} remaining {bar} {remaining:3.0f}%{extra}"


def _paths(config: Config) -> list[str]:
    source = config.source_path
    if source:
        cfg_line = f"  config: {source}"
    else:
        expected = DEFAULT_CONFIG_PATHS[0]
        cfg_line = f"  config: {expected} {ui.dim('(not found — using defaults)')}"
    data = Path(config.data_dir)
    return [
        cfg_line,
        f"  state:  {data / 'leftover-state.json'}",
        f"  ledger: {data / 'ledger.json'}",
    ]


async def run(config: Config, cached: dict | None = None,
              quotas: dict[str, Quota] | None = None) -> str:
    cached = cached if isinstance(cached, dict) else {}
    lines = ["leftover doctor"]
    width = max([10, *(len(spec.label) for spec in config.agents)])
    for spec in config.agents:
        lines.append(await _roster_line(spec, cached, width, quotas))
    on = [a.label for a in config.agents if a.enabled and a.installed]
    if on:
        lines.append(ui.dim("  on: " + " · ".join(on)))
    else:
        lines.append(ui.err("  on: none — install and log in to at least one CLI"))
    lines.append(doctor_line())
    lines.extend(_paths(config))
    return "\n".join(lines)
