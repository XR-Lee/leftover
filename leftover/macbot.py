"""Thin leftover router: classify a task, pick a subscription CLI, hand off.

Existing wheels this uses:
- leftover.quota     Codex/Grok probes, refusal classifier, local ledger
- leftover.router    fallback + circuit breaker (strategy=lag_waste)
- official CLIs      claude / codex / grok / cursor-agent  (no proxy, no keys)
- Agent Skills       SKILL.md dropped into ~/.codex ~/.claude ~/.agents
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import contextlib
import fcntl
import json
import os
import re
import select
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_mod
from . import doctor as doctor_mod
from . import intent as intent_mod
from . import quota as q
from .config import DEFAULT_DATA_DIR, LEGACY_DATA_DIR, AgentSpec, Config
from .router import Router, State
from .score import AgentScore
from .transcript import Transcript
from . import ui


class _NullPool:
    """Quota probes that need a live ACP connection just skip."""

    def peek(self, spec: AgentSpec):
        return None

    async def shutdown(self) -> None:
        return None

CU_HINT = (
    "Use computer use on this Mac: see, click, and type in GUI apps. "
    "This is not a repo-only coding task.\n\n"
)

CU_MISSING = """\
computer use needs Codex CLI (`codex`) on PATH and logged in.
Grok Bot / Cursor Cloud Agents have a computer, but it is a cloud VM.

task: {prompt}
"""

STATE_NAME = "leftover-state.json"
STATE_LOCK_TIMEOUT = 5.0
_STATE_MAP_KEYS = frozenset({"sticky", "health", "quota"})
DISCUSS = {"roundtable", "broadcast", "debate", "relay"}
DISCUSS_COMMANDS = {
    "roundtable": "/rt",
    "broadcast": "/all",
    "debate": "/debate",
    "relay": "/relay",
}

BANNER = "leftover  ·  /plan /cu /quota /cd /reset /quit"
PROGRESS_HEARTBEAT_SECONDS = 30.0

WORK = (
    "You are a subagent spawned by leftover, the user's only conversation. "
    "Do the work now in this working directory: read/edit files, run commands. "
    "Do not only describe a plan. Do not ask the user to confirm tool use. "
    "Do not run leftover — you are already the routed worker. "
    "Report what you changed when done. Do not address the user as if you "
    "are the top-level assistant."
)
PLAN_ONLY = (
    "You are a planning subagent spawned by leftover. Plan only. "
    "Do not edit files or run mutating commands. Do not run leftover. "
    "List files to touch and the change for each. Report the plan back "
    "to leftover."
)


class _Progress:
    """Keep long agent turns observable without mixing progress into answers."""

    def __init__(self, context: str, *, heartbeat_seconds: float | None = None,
                 out=None) -> None:
        self.context = context
        self.heartbeat_seconds = (
            PROGRESS_HEARTBEAT_SECONDS
            if heartbeat_seconds is None else heartbeat_seconds)
        self.out = sys.stderr if out is None else out
        self._last_visible = 0.0
        self._active = context
        self._heartbeat: asyncio.Task | None = None
        self._seated = False

    async def __aenter__(self) -> "_Progress":
        self._last_visible = asyncio.get_running_loop().time()
        if self.heartbeat_seconds > 0:
            self._heartbeat = asyncio.create_task(self._heartbeat_loop())
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._heartbeat is None:
            return
        self._heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._heartbeat

    def _touch(self) -> None:
        self._last_visible = asyncio.get_running_loop().time()

    def _line(self, message: str) -> None:
        try:
            self.out.write(message.rstrip() + "\n")
            self.out.flush()
        except (OSError, ValueError):
            return
        self._touch()

    def report(self, message: str) -> None:
        """Write and flush one human-facing progress line."""
        self._line(message)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            now = asyncio.get_running_loop().time()
            if now - self._last_visible < self.heartbeat_seconds:
                continue
            target = self._active or self.context
            self._line(f"leftover: still working ({target})")

    def sink(self, downstream=None, *, announce_attempt: bool = True,
             kind: str = "", headless: bool = False):
        """Wrap a Router sink, reporting only progress that is not answer text."""

        async def start(spec: AgentSpec):
            self._active = spec.label
            if announce_attempt and not self._seated:
                self._line(ui.seat_line(spec.label, kind, headless=headless))
                self._seated = True
            else:
                self._touch()
            on_event = await downstream(spec) if downstream is not None else None

            async def event(ev) -> None:
                kind_ev = getattr(ev, "kind", "")
                text = getattr(ev, "text", "") or ""
                if on_event is None:
                    if kind_ev == "tool" and text:
                        self._line(f"leftover: {spec.label} tool: {text[:120]}")
                else:
                    await on_event(ev)
                    if kind_ev in {"text", "tool", "error"} and text:
                        self._touch()

            return event

        return start

    def failover(self, src, dest, failure, guarded: bool) -> None:
        kind = getattr(failure, "kind", "") or ""
        self._line(ui.failover_line(
            src.label, dest.label, guarded=guarded, failure_kind=kind))


@dataclass
class Pick:
    spec: AgentSpec | None
    chain: list[str]
    scores: dict[str, AgentScore]
    reason: str
    kind: str
    prompt: str
    sticky: bool = False
    labels: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        if self.spec is not None:
            return True
        if self.kind not in DISCUSS:
            return False
        minimum = 3 if self.kind in {"debate", "relay"} else 2
        return len(self.labels) >= minimum

    @property
    def announce(self) -> str:
        if self.kind in DISCUSS and self.labels:
            return ui.announce(", ".join(self.labels), self.kind)
        return ui.announce(None if self.spec is None else self.spec.label, self.kind)

    def as_dict(self) -> dict:
        is_discussion = self.kind in DISCUSS and bool(self.labels)
        group_label = ", ".join(self.labels) if is_discussion else None
        run = (discussion_argv(self.kind, self.prompt, self.chain)
               if is_discussion and self.available else (
                   None if self.spec is None else run_argv(
                       self.spec, self.prompt, kind=self.kind)))
        scores = {
            k: {"total": round(s.total, 4), "lag": round(s.lag, 4),
                "waste": round(s.waste, 4), "detail": s.detail, "source": s.source}
            for k, s in self.scores.items()
        }
        return {
            "kind": self.kind,
            "agent": None if is_discussion or self.spec is None else self.spec.key,
            "agents": self.chain if is_discussion else None,
            "label": group_label if is_discussion else (
                None if self.spec is None else self.spec.label),
            "announce": self.announce,
            "chain": self.chain,
            "reason": self.reason,
            "sticky": self.sticky,
            "prompt": self.prompt,
            "scores": scores,
            "spawn": None if is_discussion or self.spec is None else spawn_argv(
                self.spec, self.prompt),
            "run": run,
            "completion": None if run is None else {
                "mode": "process_exit",
                "push": False,
                "max_poll_interval_seconds": 10,
            },
        }


def format_why(pick: Pick) -> str:
    """Human lag+waste table. Same shape as usher `--why`, different axis."""
    sticky = "  sticky" if pick.sticky else ""
    lines = [f"task: {pick.kind}  axis: lag+waste{sticky}", ""]
    if pick.kind in DISCUSS and pick.labels:
        lines.append("  panel: " + ", ".join(pick.labels))
        if pick.reason:
            lines.append(f"  {pick.reason}")
        return "\n".join(lines)
    width = max([8, *(len(key) for key in pick.chain)])
    lines.append(
        f"  {'agent':<{width}} {'lag':>6} {'waste':>7} {'total':>7}  "
        f"{'remaining':<16}  window")
    for key in pick.chain:
        score = pick.scores.get(key)
        mark = ("  ← launching"
                if pick.spec is not None and pick.spec.key == key else "")
        if score is None:
            lines.append(
                f"  {key:<{width}} {'—':>6} {'—':>7} {'—':>7}  "
                f"{'—':<16}  (not scored){mark}")
            continue
        if score.windows:
            best = max(score.windows, key=lambda window: window.total)
            remaining = max(0.0, 100.0 - best.used_percent)
            remain = f"{ui.remaining_bar(remaining)} {remaining:3.0f}%"
            window = (f"{best.name} {best.used_percent:.0f}% · "
                      f"{best.hours_left:.1f}h left")
        else:
            remain = "—"
            window = score.detail or "no live window"
        lines.append(
            f"  {key:<{width}} {score.lag:6.2f} {score.waste:7.3f} "
            f"{score.total:7.3f}  {remain:<16}  {window}{mark}")
    lines.append("")
    if pick.spec is not None:
        lines.append(
            f"→ {pick.spec.label}  ({pick.reason} · override with @name)")
    elif pick.reason:
        lines.append(f"→ nobody  ({pick.reason})")
    return "\n".join(lines)


def _state_path(cfg: Config) -> Path:
    return Path(cfg.data_dir) / STATE_NAME


def _state_read_paths(cfg: Config) -> list[Path]:
    data = Path(cfg.data_dir)
    paths = [data / STATE_NAME, data / "macbot-state.json"]
    try:
        if data.resolve() == DEFAULT_DATA_DIR.resolve():
            paths.extend([
                LEGACY_DATA_DIR / STATE_NAME,
                LEGACY_DATA_DIR / "macbot-state.json",
            ])
    except OSError:
        pass
    seen: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.append(path)
    return seen


class _State(dict):
    """A state snapshot that remembers what its caller actually changed."""

    def __init__(self, payload: dict) -> None:
        super().__init__(payload)
        self._baseline = copy.deepcopy(payload)

    def _commit(self, payload: dict) -> None:
        self.clear()
        self.update(copy.deepcopy(payload))
        self._baseline = copy.deepcopy(payload)


def _read_state(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return {}
        sticky = payload.get("sticky")
        if sticky is not None:
            payload["sticky"] = {
                key: value for key, value in sticky.items()
                if isinstance(key, str) and isinstance(value, str)
            } if isinstance(sticky, dict) else {}
        for key in ("health", "quota"):
            shared = payload.get(key)
            if shared is not None and not isinstance(shared, dict):
                payload[key] = {}
        return payload
    except (OSError, UnicodeError, ValueError, RecursionError):
        return {}


def load_state(cfg: Config) -> dict:
    # Writers publish with os.replace(), so a lock-free reader sees either the
    # complete old snapshot or the complete new one, never a partial JSON file.
    for path in _state_read_paths(cfg):
        if path.exists():
            return _State(_read_state(path))
    return _State({})


def _merge_state(current: dict, incoming: dict, baseline: dict) -> dict:
    """Apply the caller's changes to a freshly read on-disk snapshot.

    The three shared maps are merged per entry. This is what lets two stale
    snapshots update different working directories or agents without the last
    writer replacing the first writer's data.
    """
    merged = copy.deepcopy(current)
    for key in set(incoming) | set(baseline):
        has_new = key in incoming
        had_old = key in baseline
        new = incoming.get(key)
        old = baseline.get(key)
        if (key in _STATE_MAP_KEYS and has_new and isinstance(new, dict)
                and (not had_old or isinstance(old, dict))):
            before = old if isinstance(old, dict) else {}
            target = merged.get(key)
            target = copy.deepcopy(target) if isinstance(target, dict) else {}
            for entry in set(new) | set(before):
                if entry not in new:
                    target.pop(entry, None)
                elif entry not in before or new[entry] != before[entry]:
                    target[entry] = copy.deepcopy(new[entry])
            if target or key in merged or new:
                merged[key] = target
            continue
        if not has_new:
            if had_old:
                merged.pop(key, None)
        elif not had_old or new != old:
            merged[key] = copy.deepcopy(new)
    return merged


def _acquire_state_lock(lock_file) -> None:
    deadline = time.monotonic() + STATE_LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out locking leftover state")
            time.sleep(0.01)


def _atomic_write_state(path: Path, state: dict) -> None:
    fd, raw_tmp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def save_state(cfg: Config, state: dict) -> bool:
    """Merge and atomically persist a JSON state mapping.

    State is advisory. Invalid input, lock contention, and filesystem errors
    are deliberately reported as ``False`` instead of breaking routing.
    """
    if not isinstance(state, dict):
        return False
    path = _state_path(cfg)
    try:
        incoming = copy.deepcopy(dict(state))
        baseline = copy.deepcopy(
            state._baseline if isinstance(state, _State) else {})
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f"{path.name}.lock")
        with lock_path.open("a+b") as lock_file:
            _acquire_state_lock(lock_file)
            try:
                merged = _merge_state(_read_state(path), incoming, baseline)
                _atomic_write_state(path, merged)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001 - advisory state must not break routing
        return False
    if isinstance(state, _State):
        state._commit(merged)
    return True


def _health_blob(health) -> dict:
    return {
        "state": health.state.value,
        "until": health.until,
        "last_error": health.last_error,
        "consecutive": health.consecutive,
    }


_DEFAULT_HEALTH_BLOB = {
    "state": State.OK.value,
    "until": 0.0,
    "last_error": "",
    "consecutive": 0,
}


def restore_health(router: Router, state: dict) -> None:
    baseline_health: dict[str, dict] = {}
    baseline_quota: dict[str, dict] = {}
    if not isinstance(state, dict):
        router._persisted_health = baseline_health
        router._persisted_quota = baseline_quota
        return
    stored_health = state.get("health")
    stored_health = stored_health if isinstance(stored_health, dict) else {}
    for key, blob in stored_health.items():
        if not isinstance(blob, dict):
            continue
        spec = router.config.find(key)
        if spec is None:
            continue
        health = router.h(spec)
        try:
            health.state = State(blob.get("state", "ok"))
        except (TypeError, ValueError):
            health.state = State.OK
        try:
            health.until = float(blob.get("until") or 0.0)
        except (TypeError, ValueError):
            health.until = 0.0
        health.last_error = str(blob.get("last_error") or "")
        try:
            health.consecutive = int(blob.get("consecutive") or 0)
        except (TypeError, ValueError):
            health.consecutive = 0
        baseline_health[key] = _health_blob(health)
        health.refresh()
    stored_quota = state.get("quota")
    stored_quota = stored_quota if isinstance(stored_quota, dict) else {}
    for key, blob in stored_quota.items():
        spec = router.config.find(key)
        if spec is None:
            continue
        quota = q.Quota.from_dict(blob)
        if quota is None:
            continue
        health = router.h(spec)
        health.quota = quota
        try:
            health.quota_checked = float(
                blob.get("checked_at") or quota.checked_at or 0)
        except (TypeError, ValueError):
            health.quota_checked = quota.checked_at
        persisted = quota.to_dict()
        persisted["checked_at"] = health.quota_checked or quota.checked_at
        baseline_quota[key] = persisted
    sticky = state.get("sticky")
    sticky = sticky if isinstance(sticky, dict) else {}
    router.last_success = sticky.get(os.getcwd())
    router._persisted_health = baseline_health
    router._persisted_quota = baseline_quota


def persist_health(cfg: Config, router: Router, state: dict) -> None:
    if not isinstance(state, dict):
        return
    baseline_health = getattr(router, "_persisted_health", {})
    health_state = state.get("health")
    health_state = dict(health_state) if isinstance(health_state, dict) else {}
    current_health: dict[str, dict] = {}
    for key, health in router.health.items():
        blob = _health_blob(health)
        current_health[key] = blob
        before = baseline_health.get(key)
        if blob != before and (before is not None or blob != _DEFAULT_HEALTH_BLOB):
            health_state[key] = blob
    if health_state:
        state["health"] = health_state

    baseline_quota = getattr(router, "_persisted_quota", {})
    cached = state.get("quota")
    cached = dict(cached) if isinstance(cached, dict) else {}
    current_quota: dict[str, dict] = {}
    for key, h in router.health.items():
        if h.quota is None:
            continue
        blob = h.quota.to_dict()
        blob["checked_at"] = h.quota_checked or h.quota.checked_at
        current_quota[key] = blob
        if blob != baseline_quota.get(key):
            cached[key] = blob
    if cached:
        state["quota"] = cached
    if save_state(cfg, state):
        router._persisted_health = current_health
        router._persisted_quota = {
            **baseline_quota,
            **current_quota,
        }


def spawn_argv(spec: AgentSpec, prompt: str) -> list[str]:
    argv = spec.launch
    if spec.prompt_as_arg and prompt.strip():
        argv = argv + [prompt]
    return argv


def run_argv(spec: AgentSpec, prompt: str, *, kind: str = "coding") -> list[str]:
    """Headless handoff that prints the subagent's answer to stdout."""
    argv = ["leftover", "--print", "--use", spec.key]
    if kind == "plan":
        argv.append("--plan")
    elif kind == "computer_use":
        argv.append("--cu")
    if prompt.strip():
        argv.append(prompt)
    return argv


def discussion_argv(kind: str, prompt: str, chain: list[str]) -> list[str]:
    command = DISCUSS_COMMANDS[kind]
    mentions = " ".join(f"@{key}" for key in chain)
    task = " ".join(part for part in (command, mentions, prompt.strip()) if part)
    return ["leftover", "--print", task]


def prepare_task(text: str, *, plan: bool = False, cu: bool = False) -> str:
    text = text.strip()
    command = text.split(maxsplit=1)[0].lower() if text else ""
    if plan and text and command not in intent_mod.PLAN_PREFIXES:
        text = "/plan " + text
        command = "/plan"
    if cu and text and command not in intent_mod.CU_PREFIXES:
        text = "/cu " + text
    return text


def apply_use(parsed: intent_mod.Intent, use: str | None) -> intent_mod.Intent:
    """`--use` forces a backend first. `--agent` (caller identity) must not."""
    if not use:
        return parsed
    key = use.lower().lstrip("@").strip()
    if not key:
        return parsed
    return intent_mod.Intent(
        kind=parsed.kind,
        prompt=parsed.prompt,
        named=key,
        raw=parsed.raw,
        named_all=parsed.named_all,
    )


def _usable(router: Router, spec: AgentSpec) -> bool:
    return spec.enabled and spec.installed and router.h(spec).usable


def _has_live_session(router: Router, spec: AgentSpec) -> bool:
    """Sticky routing is only meaningful while this process owns the session."""
    try:
        runner = router.pool.peek(spec)
        return bool(runner is not None and runner.live_session())
    except Exception:                              # noqa: BLE001 - routing degrades
        return False


def _chain_specs(cfg: Config, pick: Pick) -> list[AgentSpec]:
    specs: list[AgentSpec] = []
    for key in pick.chain:
        spec = cfg.find(key)
        if spec is not None and spec not in specs:
            specs.append(spec)
    return specs


async def decide(cfg: Config, parsed: intent_mod.Intent, cwd: str,
                 router: Router | None = None) -> Pick:
    router = router or Router(cfg, _NullPool())
    cfg.routing.strategy = "lag_waste"
    state = load_state(cfg)
    restore_health(router, state)

    named = cfg.find(parsed.named) if parsed.named else None
    mentioned = list(parsed.named_all or [])
    if parsed.named and parsed.named not in mentioned:
        mentioned.insert(0, parsed.named)
    unknown = [token for token in mentioned if cfg.find(token) is None]
    if unknown:
        names = ", ".join(f"@{token}" for token in unknown)
        return Pick(None, [], {}, f"unknown agent {names}",
                    parsed.kind, parsed.prompt)

    if parsed.kind in DISCUSS:
        if not parsed.prompt.strip():
            return Pick(None, [], {}, f"{parsed.kind} needs a topic",
                        parsed.kind, parsed.prompt)
        from .orchestrator import Orchestrator
        orch = Orchestrator(cfg, _NullPool(), router)
        panel = (orch.debate_panel(parsed.named_all)
                 if parsed.kind == "debate"
                 else orch.discussion_panel(parsed.named_all))
        if parsed.kind == "relay":
            panel = panel[:3]
        minimum = 3 if parsed.kind in {"debate", "relay"} else 2
        if len(panel) < minimum:
            return Pick(None, [spec.key for spec in panel], {},
                        f"{parsed.kind} needs at least {minimum} installed CLIs",
                        parsed.kind, parsed.prompt,
                        labels=[spec.label for spec in panel])
        return Pick(None, [spec.key for spec in panel], {},
                    f"{parsed.kind} panel",
                    parsed.kind, parsed.prompt,
                    labels=[spec.label for spec in panel])

    if parsed.kind == "computer_use":
        cu = cfg.cu_agent()
        prompt = parsed.prompt
        if not prompt.strip():
            return Pick(None, [], {}, "computer use needs a task",
                        parsed.kind, prompt)
        if prompt.strip() and not prompt.startswith(CU_HINT):
            prompt = CU_HINT + prompt
        if cu is None or not cu.installed:
            return Pick(None, [], {}, "computer use → Codex CLI, but `codex` is missing",
                        parsed.kind, prompt)
        if not router.h(cu).usable:
            return Pick(None, [cu.key], {},
                        f"computer use → Codex, but it is {router.h(cu).describe()}",
                        parsed.kind, prompt)
        return Pick(cu, [cu.key], {}, "computer use → Codex CLI",
                    parsed.kind, prompt)

    if parsed.kind == "plan" and not parsed.prompt.strip():
        return Pick(None, [], {}, "plan needs a task",
                    parsed.kind, parsed.prompt)

    coding = [s for s in cfg.coding_agents() if s.installed]
    plan = cfg.plan_agent()
    ranked = await router.rank(coding) if coding else []
    scores = dict(getattr(router, "last_scores", {}) or {})

    sticky_key = router.conversation_success
    sticky_spec = cfg.find(sticky_key) if sticky_key else None
    used_sticky = False
    if (parsed.kind == "coding" and named is None and sticky_spec is not None
            and sticky_spec in ranked and _usable(router, sticky_spec)
            and _has_live_session(router, sticky_spec)):
        ranked = [sticky_spec] + [s for s in ranked if s.key != sticky_spec.key]
        used_sticky = True

    chain: list[AgentSpec] = []

    def push(spec: AgentSpec | None) -> None:
        if spec is None or spec in chain:
            return
        if spec.enabled:
            chain.append(spec)

    if named is not None:
        push(named)
        for spec in ranked:
            push(spec)
        push(plan)
        reason = f"named @{named.key}"
    elif parsed.kind == "plan":
        push(plan)
        for spec in ranked:
            push(spec)
        reason = f"plan → {cfg.routing.plan_key}, coding pool as fallback"
    else:
        for spec in ranked:
            push(spec)
        push(plan)
        if used_sticky:
            reason = f"sticky {sticky_spec.key} (live session)"
        elif ranked:
            top = scores.get(ranked[0].key)
            reason = top.detail if top else f"lag_waste → {ranked[0].key}"
        else:
            reason = "no coding CLI installed"

    installed_chain = [s for s in chain if s.installed]
    usable_chain = [s for s in installed_chain if router.h(s).usable]
    chosen = usable_chain[0] if usable_chain else None
    if chosen is None and installed_chain:
        reason = "every installed CLI is benched; " + reason
    persist_health(cfg, router, state)
    return Pick(chosen, [s.key for s in installed_chain], scores, reason,
                parsed.kind, parsed.prompt, sticky=used_sticky)


async def _decide_with_progress(
        cfg: Config, parsed: intent_mod.Intent, cwd: str, *,
        heartbeat_seconds: float | None = None) -> Pick:
    """Make quota probing/ranking visible for human-facing headless runs."""
    async with _Progress(
            "routing", heartbeat_seconds=heartbeat_seconds) as progress:
        progress.report("leftover: routing...")
        return await decide(cfg, parsed, cwd)


def _seat_reason(pick: Pick) -> str:
    if pick.kind == "coding" and not pick.sticky:
        return "lag+waste"
    return ""


def _print_pick(pick: Pick, *, verbose: bool = False, file=None,
                headless: bool = False) -> None:
    out = sys.stdout if file is None else file
    if verbose:
        out.write(format_why(pick) + "\n")
        return
    if pick.kind in DISCUSS and pick.labels:
        if pick.available:
            out.write(ui.seat_line(", ".join(pick.labels), pick.kind,
                                   headless=headless) + "\n")
        else:
            out.write(ui.err(pick.announce) + "\n")
        return
    if (pick.kind == "computer_use" and pick.spec is None
            and "`codex` is missing" in pick.reason):
        out.write(CU_MISSING.format(prompt=pick.prompt or "(none)"))
        return
    if pick.spec is None:
        out.write(ui.err(pick.announce) + "\n")
        if pick.reason:
            out.write(ui.dim(f"  {pick.reason}") + "\n")
        return
    out.write(ui.seat_line(
        pick.spec.label, pick.kind, headless=headless,
        sticky=pick.sticky, reason=_seat_reason(pick)) + "\n")


def _compose(spec: AgentSpec, prompt: str, transcript: Transcript,
             followup: bool, kind: str) -> str:
    """First turn on a harness gets instructions; later turns are just the user.

    Native ACP sessions *are* the context harness. Re-pasting the whole
    transcript on every follow-up fights that.
    """
    if followup:
        return prompt
    parts: list[str] = [PLAN_ONLY if kind == "plan" else WORK]
    if spec.persona.strip():
        parts.append(spec.persona.strip())
    history = transcript.render()
    if history:
        parts.append("--- earlier leftover conversation ---\n" + history)
    parts.append("--- user ---\n" + prompt)
    return "\n\n".join(parts)


async def _speak(cfg: Config, router: Router, transcript: Transcript,
                 pick: Pick, user_line: str, *,
                 heartbeat_seconds: float | None = None) -> None:
    if pick.spec is None:
        _print_pick(pick)
        return
    _print_pick(pick)
    transcript.add("You", user_line)
    exclude = {a.key for a in cfg.enabled_agents() if a.key not in pick.chain}
    prompt = pick.prompt or user_line
    primary = pick.spec.key
    async def sink(spec: AgentSpec):
        return ui.StreamSink(
            spec.label, out=sys.stdout, show_header=spec.key != primary)

    def prompt_for(spec: AgentSpec) -> str:
        runner = router.pool.peek(spec)
        followup = bool(runner is not None and runner.live_session())
        return _compose(spec, prompt, transcript, followup, pick.kind)

    def on_failover(src, dest, failure, guarded: bool) -> None:
        sys.stdout.write(ui.failover_line(
            src.label, dest.label, guarded=guarded,
            failure_kind=getattr(failure, "kind", "") or "") + "\n")
        sys.stdout.flush()

    ordered_chain = _chain_specs(cfg, pick)
    async with _Progress(
            f"{pick.kind} route", heartbeat_seconds=heartbeat_seconds) as progress:
        turn, decision = await router.run(
            prompt_for,
            primary=pick.spec,
            sink=progress.sink(sink, announce_attempt=False, kind=pick.kind),
            max_attempts=cfg.routing.max_attempts,
            exclude=exclude,
            ordered_chain=ordered_chain,
            on_failover=on_failover,
        )
    if turn.ok:
        transcript.add(turn.agent.label, turn.text)
    elif turn.error:
        sys.stdout.write(ui.err(f"  {turn.error}") + "\n")
    if decision.describe():
        sys.stdout.write(ui.dim(f"  {decision.describe()}") + "\n")
    elif turn.ok and turn.seconds:
        n_tools = len(turn.tools)
        meta = f"{turn.seconds:.0f}s"
        if n_tools:
            meta += f" · {n_tools} tool" + ("s" if n_tools != 1 else "")
        sys.stdout.write(ui.dim(f"  {meta}") + "\n")
    sys.stdout.write("\n")
    state = load_state(cfg)
    if decision.chosen is not None:
        state.setdefault("sticky", {})[os.getcwd()] = decision.chosen.key
        router.last_success = decision.chosen.key
        router.conversation_success = decision.chosen.key
    persist_health(cfg, router, state)


async def _discuss(cfg: Config, router: Router, transcript: Transcript,
                   parsed: intent_mod.Intent, *,
                   heartbeat_seconds: float | None = None) -> bool:
    """Heterogeneous subagents, each seeing the others."""
    from .orchestrator import Orchestrator, Plan, summarise

    orch = Orchestrator(cfg, router.pool, router)
    orch.transcript = transcript
    names = parsed.named_all if parsed.named_all else None
    unknown = [token for token in (names or []) if cfg.find(token) is None]
    if unknown:
        labels = ", ".join(f"@{token}" for token in unknown)
        sys.stdout.write(ui.err(f"  unknown agent {labels}") + "\n")
        return False
    panel = (orch.debate_panel(names) if parsed.kind == "debate"
             else orch.discussion_panel(names))
    if parsed.kind == "broadcast":
        mode, agents = "broadcast", panel
    elif parsed.kind == "debate":
        mode, agents = "debate", panel[:3]
    elif parsed.kind == "relay":
        mode, agents = "relay", panel[:3]
    else:
        mode, agents = "roundtable", panel
    if not parsed.prompt.strip():
        sys.stdout.write(ui.err("  need a topic") + "\n")
        return False
    minimum = 3 if mode in {"debate", "relay"} else 2
    if len(agents) < minimum:
        sys.stdout.write(ui.err(
            f"  {mode} needs at least {minimum} installed CLIs") + "\n")
        return False
    names = ", ".join(a.label for a in agents)
    sys.stdout.write(ui.seat_line(names, mode) + "\n")

    async def sink(spec: AgentSpec):
        return ui.StreamSink(spec.label, out=sys.stdout)

    async with _Progress(
            f"{mode} panel", heartbeat_seconds=heartbeat_seconds) as progress:
        turns = await orch.execute(Plan(mode, parsed.prompt, agents, {
            "rounds": str(cfg.debate_rounds)} if mode == "debate" else {}),
            progress.sink(sink, announce_attempt=mode != "debate"))
    sys.stdout.write(ui.dim(f"  {summarise(turns)}") + "\n\n")
    persist_health(cfg, router, load_state(cfg))
    return bool(turns) and all(
        turn.ok and not turn.meta.get("delivery_error") for turn in turns)


async def run_discuss(cfg: Config, parsed: intent_mod.Intent, *,
                      heartbeat_seconds: float | None = None) -> int:
    """Headless group execution for `--print /rt|/all|/debate|/relay`."""
    from .agents import AgentPool
    cfg.routing.strategy = "lag_waste"
    pool = AgentPool(cfg)
    await pool.set_workdir(os.getcwd())
    router = Router(cfg, pool)
    restore_health(router, load_state(cfg))
    try:
        ok = await _discuss(cfg, router, Transcript(keep=cfg.transcript_turns),
                            parsed, heartbeat_seconds=heartbeat_seconds)
        return 0 if ok else 1
    finally:
        await pool.shutdown()


async def chat(cfg: Config, first: str = "") -> int:
    """Stay in one conversation. Each line is routed; history is shared."""
    from .agents import AgentPool
    cfg.routing.strategy = "lag_waste"
    pool = AgentPool(cfg)
    await pool.set_workdir(os.getcwd())
    router = Router(cfg, pool)
    restore_health(router, load_state(cfg))
    transcript = Transcript(keep=cfg.transcript_turns)
    ui.setup_readline(Path(cfg.data_dir) / "history")
    folder = Path(pool.workdir).name or pool.workdir
    print(ui.bold("leftover") + ui.dim(f"  {folder}"))
    on = [a.label for a in cfg.agents if a.enabled and a.installed]
    off = [a.label for a in cfg.agents if a.enabled and not a.installed]
    roster = " · ".join(on) if on else "nobody on PATH"
    if off:
        roster += ui.dim("  off: " + " · ".join(off))
    print(ui.dim("  ") + roster)
    print(ui.dim("  /plan /cu /rt /debate /relay   /quota /cd /quit") + "\n")
    loop = asyncio.get_running_loop()

    async def handle(line: str) -> bool:
        raw = line.strip()
        if not raw:
            return True
        if raw in ("/quit", "/exit"):
            return False
        if raw in ("/help", "?"):
            print(ui.dim("  task  /plan  /cu  /rt  /debate  /relay  /all"))
            print(ui.dim("  @name  @claude @gpt …  /quota /cd /reset /quit"))
            return True
        if raw == "/who":
            for a in cfg.agents:
                mark = ui.ok("on") if a.installed else ui.dim("off")
                print(f"  {mark}  {a.label}  @{a.key}")
            return True
        if raw == "/quota":
            print(await router.report())
            persist_health(cfg, router, load_state(cfg))
            return True
        if raw == "/reset":
            transcript.clear()
            await pool.shutdown()
            print(ui.dim("  cleared"))
            return True
        if raw.startswith("/cd"):
            target = Path(os.path.expanduser(raw[3:].strip() or ".")).resolve()
            if target.is_dir():
                await pool.set_workdir(str(target))
                os.chdir(target)
                print(ui.dim(f"  {target}"))
            else:
                print(ui.err(f"  no such directory: {target}"))
            return True
        parsed = intent_mod.parse(raw)
        if parsed.kind in DISCUSS:
            await _discuss(cfg, router, transcript, parsed)
            return True
        user_text = parsed.prompt or raw
        pick = await decide(cfg, parsed, os.getcwd(), router)
        await _speak(cfg, router, transcript, pick, user_text)
        return True

    try:
        if first.strip():
            if not await handle(first):
                return 0
        while True:
            try:
                line = await loop.run_in_executor(None, input, ui.dim("you> "))
            except (EOFError, KeyboardInterrupt):
                break
            if not await handle(line):
                break
    finally:
        await pool.shutdown()
    return 0


def _exec(spec: AgentSpec, prompt: str) -> int:
    argv = spawn_argv(spec, prompt)
    binary = shutil.which(argv[0]) or argv[0]
    if shutil.which(argv[0]) is None:
        sys.stderr.write(f"leftover: `{argv[0]}` is not on PATH\n")
        return 127
    if not spec.prompt_as_arg and prompt.strip():
        sys.stderr.write(
            f"leftover: {spec.key} TUI does not take an argv prompt.\n"
            f"paste this after it starts:\n\n{prompt}\n\n")
    os.execvp(binary, [binary, *argv[1:]])
    return 1


_DURATION = re.compile(
    r"^(?:(?P<h>\d+(?:\.\d+)?)h)?(?:(?P<m>\d+(?:\.\d+)?)m)?(?:(?P<s>\d+(?:\.\d+)?)s)?$"
)


def parse_duration(text: str) -> float:
    """usher `--timeout` values: `90s`, `2m`, `5m30s`, or a raw second count."""
    raw = str(text).strip().lower()
    if raw.replace(".", "", 1).isdigit():
        seconds = float(raw)
    else:
        matched = _DURATION.fullmatch(raw)
        if not matched or not any(matched.group(part) for part in ("h", "m", "s")):
            raise argparse.ArgumentTypeError(
                f"invalid duration {text!r} (try 90s, 2m, 5m)")
        seconds = (
            float(matched.group("h") or 0) * 3600
            + float(matched.group("m") or 0) * 60
            + float(matched.group("s") or 0)
        )
    if seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive")
    return seconds


_PIPED_STDIN_READ_SECONDS = 0.1
_PIPED_STDIN_CHUNK_BYTES = 64 * 1024


def read_piped_stdin() -> str:
    """Buffer piped stdin so a failover attempt sees the same extra context.

    Drain bytes that arrive within one short bounded interval. A readable pipe
    is not necessarily at EOF, so a text-mode read-to-EOF can otherwise wait
    forever while its writer remains open.
    """
    blocking: bool | None = None
    fd: int | None = None
    try:
        if sys.stdin.isatty():
            return ""
        fd = sys.stdin.fileno()
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            return ""
        blocking = os.get_blocking(fd)
        if blocking:
            os.set_blocking(fd, False)
        deadline = time.monotonic() + _PIPED_STDIN_READ_SECONDS
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(fd, _PIPED_STDIN_CHUNK_BYTES)
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([fd], [], [], remaining)
                if not ready:
                    break
                continue
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
            if time.monotonic() >= deadline:
                break
        encoding = getattr(sys.stdin, "encoding", None) or "utf-8"
        errors = getattr(sys.stdin, "errors", None) or "replace"
        return b"".join(chunks).decode(encoding, errors)
    except (OSError, ValueError, AttributeError):
        return ""
    finally:
        if fd is not None and blocking:
            with contextlib.suppress(OSError):
                os.set_blocking(fd, True)


def _print_envelope(pick: Pick, turn, decision, *, exit_code: int) -> None:
    chosen = decision.chosen or (turn.agent if turn is not None else None)
    blob = {
        "agent": None if chosen is None else chosen.key,
        "kind": pick.kind,
        "exit_code": exit_code,
        "output": "" if turn is None else (turn.text or ""),
        "attempts": [attempt.as_dict() for attempt in decision.attempts],
    }
    sys.stdout.write(json.dumps(blob, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _apply_timeout(cfg: Config, seconds: float | None) -> None:
    if seconds is None:
        return
    for spec in cfg.agents:
        spec.timeout = seconds


async def run_print(cfg: Config, pick: Pick, *,
                    heartbeat_seconds: float | None = None,
                    as_json: bool = False,
                    stdin_extra: str = "") -> int:
    """Headless: try the chain, fallback on classified refusal."""
    if pick.spec is None:
        sys.stderr.write(pick.announce + "\n")
        if pick.reason:
            sys.stderr.write(f"  {pick.reason}\n")
        sys.stderr.flush()
        return 2
    from .agents import AgentPool
    pool = AgentPool(cfg)
    try:
        await pool.set_workdir(os.getcwd())
        router = Router(cfg, pool)
        restore_health(router, load_state(cfg))
        prompt = pick.prompt or "Continue."
        extra = stdin_extra.strip()
        if extra:
            prompt = prompt + "\n\n--- stdin ---\n" + extra
        ordered_chain = _chain_specs(cfg, pick)

        def prompt_for(spec: AgentSpec) -> str:
            return _compose(spec, prompt, Transcript(keep=0), followup=False,
                            kind=pick.kind)

        async with _Progress(
                f"{pick.kind} route",
                heartbeat_seconds=heartbeat_seconds) as progress:
            turn, decision = await router.run(
                prompt_for, primary=pick.spec,
                sink=progress.sink(kind=pick.kind, headless=True),
                max_attempts=cfg.routing.max_attempts,
                ordered_chain=ordered_chain,
                on_failover=progress.failover)
        state = load_state(cfg)
        if decision.chosen is not None:
            sticky = state.setdefault("sticky", {})
            sticky[os.getcwd()] = decision.chosen.key
        persist_health(cfg, router, state)
        timed_out = bool(turn.meta.get("timeout_kind")) or (
            decision.attempts[-1].timed_out if decision.attempts else False)
        exit_code = 0 if turn.ok else (124 if timed_out else 1)
        if as_json:
            _print_envelope(pick, turn, decision, exit_code=exit_code)
        elif turn.ok and turn.text.strip():
            sys.stdout.write(turn.text.rstrip() + "\n")
            sys.stdout.flush()
        elif turn.error:
            sys.stderr.write(f"leftover: {turn.error.rstrip()}\n")
        if decision.describe():
            sys.stderr.write(f"routed: {decision.describe()}\n")
        sys.stderr.flush()
        return exit_code
    finally:
        await pool.shutdown()


def _skill_source() -> Path:
    return Path(__file__).resolve().parent / "skills" / "leftover" / "SKILL.md"


def skill_destinations() -> list[Path]:
    return [
        Path.home() / ".codex" / "skills" / "leftover" / "SKILL.md",
        Path.home() / ".agents" / "skills" / "leftover" / "SKILL.md",
        Path.home() / ".claude" / "skills" / "leftover" / "SKILL.md",
        Path.home() / ".grok" / "skills" / "leftover" / "SKILL.md",
        Path.home() / ".cursor" / "skills" / "leftover" / "SKILL.md",
    ]


def link_skill(src: Path, dest: Path) -> Path:
    """Point dest at src. Replace a copied file so later edits stay in sync."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = src.resolve()
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    dest.symlink_to(target)
    return dest


def install_skills() -> str:
    src = _skill_source()
    if not src.is_file():
        return f"skill file missing: {src}"
    written = [str(link_skill(src, dest)) for dest in skill_destinations()]
    return "linked:\n" + "\n".join(f"  {p}" for p in written)


def _parse_argv(argv: list[str] | None) -> argparse.Namespace:
    # Allow `leftover quota` and `leftover fix the tests` without subcommands colliding.
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"quota", "doctor", "install-skills"}
    if argv and argv[0] in commands:
        ns = argparse.Namespace(
            command=argv[0], prompt=[], config=None, pick=False, headless=False,
            dry_run=False, why=False, plan=False, cu=False, agent=None,
            use=None, json=False, tui=False, timeout=None)
        rest = argv[1:]
        if rest and rest[0] in ("--config", "-c"):
            if len(rest) < 2:
                raise SystemExit(f"leftover {argv[0]}: {rest[0]} needs a path")
            ns.config = rest[1]
        return ns
    p = argparse.ArgumentParser(prog="leftover")
    p.add_argument("--config", "-c", default=None)
    p.add_argument("--pick", action="store_true")
    p.add_argument("-p", "--print", dest="headless", action="store_true",
                   help="headless: answer on stdout, chatter on stderr")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--why", action="store_true",
                   help="print the lag+waste table and stop (usher-shaped)")
    p.add_argument("--plan", action="store_true")
    p.add_argument("--cu", action="store_true")
    p.add_argument("--agent", default=None,
                   help="caller identity (who is asking). Does not force routing")
    p.add_argument("--use", default=None,
                   help="force this backend first (same as @name)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", default=None, type=parse_duration,
                   help="headless: kill the agent after this long (90s, 2m)")
    p.add_argument("--tui", action="store_true",
                   help="hand off to that CLI's own UI (no leftover conversation)")
    p.add_argument("prompt", nargs="*")
    ns = p.parse_args(argv)
    ns.command = "pick" if ns.pick else None
    return ns


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        raise SystemExit("leftover needs Python 3.10+")
    args = _parse_argv(argv)
    if args.command == "install-skills":
        print(install_skills())
        return 0

    cfg = config_mod.load(args.config)
    if getattr(args, "timeout", None) is not None and not args.headless:
        print("leftover: --timeout requires --print/-p", file=sys.stderr)
        return 2
    _apply_timeout(cfg, getattr(args, "timeout", None))
    if args.command == "doctor":
        cached = load_state(cfg).get("quota") or {}
        print(asyncio.run(doctor_mod.run(cfg, cached if isinstance(cached, dict) else {})))
        return 0
    if args.command == "quota":
        cfg.routing.strategy = "lag_waste"
        router = Router(cfg, _NullPool())
        restore_health(router, load_state(cfg))
        print(asyncio.run(router.report()))
        persist_health(cfg, router, load_state(cfg))
        return 0

    text = prepare_task(" ".join(args.prompt), plan=args.plan, cu=args.cu)

    dump_pick = (
        (args.json and not args.headless)
        or args.command == "pick" or args.pick
    )
    if (dump_pick or args.dry_run or args.why or args.headless or args.tui):
        if not text:
            print("usage: leftover [--pick|--dry-run|--why|--print|-p|--tui] <task>",
                  file=sys.stderr)
            return 2
        parsed = apply_use(intent_mod.parse(text), args.use)
        show_routing_progress = (
            args.headless
            and not dump_pick
            and not args.dry_run and not args.why
            and parsed.kind not in DISCUSS
        )
        route = (_decide_with_progress(cfg, parsed, os.getcwd())
                 if show_routing_progress
                 else decide(cfg, parsed, os.getcwd()))
        pick = asyncio.run(route)
        if dump_pick:
            blob = pick.as_dict()
            if args.agent:
                blob["self"] = args.agent
            print(json.dumps(blob, indent=2, ensure_ascii=False))
            return 0 if pick.available else 2
        if args.why:
            print(format_why(pick))
            return 0 if pick.available else 2
        if args.dry_run:
            _print_pick(pick, verbose=True)
            return 0 if pick.available else 2
        if args.headless and parsed.kind in DISCUSS:
            return asyncio.run(run_discuss(cfg, parsed))
        if pick.spec is None:
            _print_pick(pick, file=sys.stderr if args.headless or args.tui else None,
                        headless=args.headless)
            return 2
        if args.headless:
            return asyncio.run(run_print(
                cfg, pick, as_json=args.json,
                stdin_extra=read_piped_stdin()))
        state = load_state(cfg)
        state.setdefault("sticky", {})[os.getcwd()] = pick.spec.key
        save_state(cfg, state)
        _print_pick(pick, file=sys.stderr, headless=False)
        sys.stdout.flush()
        sys.stderr.flush()
        return _exec(pick.spec, pick.prompt)

    return asyncio.run(chat(cfg, first=text))


if __name__ == "__main__":
    raise SystemExit(main())
