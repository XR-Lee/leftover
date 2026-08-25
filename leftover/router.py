"""Which agent should answer this, and what happens when it can't.

Two jobs, deliberately kept separate:

*Routing*   picks the best agent for a turn from quota headroom, declared
            priority, cost tier and recent latency.
*Fallback*  is what saves you when routing was wrong anyway: a refusal is
            classified, the agent is put in the right kind of penalty box,
            and the next candidate takes the turn - within the same request,
            so the chat never sees the failure.

Fallback is the part that always works. Routing quality depends on how much
each CLI is willing to tell us (see quota.py), so the router degrades to
"try them in order" when nobody reports anything.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import re
import threading
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from . import quota as q
from .agents import AgentPool, OnEvent, Turn
from .config import AgentSpec, Config

# Stand-in so an error Turn always has something to name itself after.
NOBODY = AgentSpec(key="none", label="nobody", emoji="-")

# usher prefixes failover prompts the same way: the first agent may have
# already edited files. Keep this in the router so REPL, --print, and
# group substitution all see it.
CONTINUATION_GUARD = (
    "A previous agent was already working on this task and may have left "
    "partial edits. Inspect `git status` before continuing.\n\n"
)

log = logging.getLogger("leftover.router")


def _consume_async_result(task: asyncio.Future[Any]) -> None:
    try:
        task.exception()
    except BaseException:
        pass


async def await_bounded(awaitable: Awaitable[Any], timeout: float) -> Any:
    """Return at a caller deadline without waiting on callback cleanup."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=max(timeout, 0.0))
    except BaseException:
        if not task.done():
            task.cancel()
        task.add_done_callback(_consume_async_result)
        raise
    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_async_result)
        raise TimeoutError
    return task.result()


class _QuotaProbeBusy(RuntimeError):
    pass


class _DaemonProbePool:
    """Small bounded worker pool that never joins during event-loop shutdown."""

    def __init__(self, workers: int = 4, queue_size: int = 16) -> None:
        self._queue: queue.Queue[tuple[
            concurrent.futures.Future[Any], Callable[..., Any],
            tuple[Any, ...], dict[str, Any],
        ]] = queue.Queue(maxsize=queue_size)
        for index in range(workers):
            threading.Thread(
                target=self._worker,
                name=f"leftover-quota-{index + 1}",
                daemon=True,
            ).start()

    def submit(self, fn: Callable[..., Any], *args: Any,
               **kwargs: Any) -> concurrent.futures.Future[Any]:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        try:
            self._queue.put_nowait((future, fn, args, kwargs))
        except queue.Full:
            future.set_exception(_QuotaProbeBusy("quota probe workers are busy"))
        return future

    def _worker(self) -> None:
        while True:
            future, fn, args, kwargs = self._queue.get()
            try:
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:  # preserve the probe's exception
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._queue.task_done()


_PROBE_POOL: _DaemonProbePool | None = None
_PROBE_POOL_LOCK = threading.Lock()


def _probe_pool() -> _DaemonProbePool:
    global _PROBE_POOL
    if _PROBE_POOL is None:
        with _PROBE_POOL_LOCK:
            if _PROBE_POOL is None:
                _PROBE_POOL = _DaemonProbePool()
    return _PROBE_POOL


async def _run_sync_probe(fn: Callable[..., Any], *args: Any,
                          timeout: float) -> Any:
    """Run a sync probe without enrolling it in asyncio's default executor."""
    if timeout <= 0:
        raise TimeoutError("quota probe deadline expired")
    wrapped = asyncio.wrap_future(_probe_pool().submit(fn, *args))
    try:
        done, _ = await asyncio.wait({wrapped}, timeout=timeout)
    except BaseException:
        wrapped.cancel()
        raise
    if wrapped not in done:
        wrapped.cancel()
        raise TimeoutError(f"quota probe timed out after {timeout:.2f}s")
    return wrapped.result()

_RESULT_REFUSALS = {
    "quota": re.compile(
        r"(?:error:\s*)?(?:"
        r"you(?:'ve| have) hit your (?:weekly|session|usage|monthly) limit|"
        r"you(?:'ve| have) hit your (?:opus|sonnet|haiku|fable)[\w\s]* limit|"
        r"(?:weekly|session|usage|message|monthly|spend) limit (?:has been )?reached|"
        r"quota (?:exceeded|reached)|out of (?:credits|requests)|"
        r"(?:your )?credit balance is too low)\b"
        r"(?:\s*[.!])?"
        r"(?:\s*(?:[\u00b7:;,.!()\[\]-]\s*)?(?:"
        r"resets?\b.{0,160}|try again\b.{0,160}|retry\b.{0,160}|"
        r"available\b.{0,160}|"
        r"(?:daily|weekly|monthly|session|model|plan)\b.{0,80}\bresets?\b.{0,80}|"
        r"please (?:wait|try again|upgrade|purchase|add credits|contact support)\b.{0,160}|"
        r"(?:upgrade|purchase|add credits|contact support)\b.{0,160}))?\s*",
        re.I,
    ),
    "auth": re.compile(
        r"(?:(?:api|authentication|authorization)?\s*error(?:\s*\(?401\)?)?:\s*)?"
        r"(?:(?:you are )?(?:not logged in|not authenticated)|"
        r"please (?:log ?in|sign ?in)|unauthorized\b|"
        r"invalid (?:api key|credentials)\b|authentication required\b|"
        r"401\s+unauthorized)"
        r"(?:\s*[.!])?"
        r"(?:\s*(?:[:;,.!()-]\s*)?(?:"
        r"to continue\b.{0,160}|and try again\b.{0,160}|"
        r"please (?:log ?in|sign ?in|authenticate|run\b.{0,80}\blog ?in)\b.{0,160}|"
        r"run\b.{0,80}\b(?:log ?in|authenticate)\b.{0,80}))?\s*",
        re.I,
    ),
    "rate_limit": re.compile(
        r"(?:(?:api )?error:\s*)?(?:"
        r"request rejected\s*\(?429\)?|"
        r"429\s+too many requests\b|"
        r"429\s*[:.-]\s*rate limit (?:exceeded|reached)\b|"
        r"429(?:\s+rate limit(?:ed| exceeded| reached)?)?\b|"
        r"529\s+(?:overloaded|server error)\b|"
        r"too many requests\b|rate limit (?:exceeded|reached)\b|"
        r"rate limit\b|rate limited\b|temporarily limiting requests\b|"
        r"(?:the )?(?:service|server) is overloaded\b)"
        r"(?:\s*[.!])?"
        r"(?:\s*(?:[:;,.!()-]\s*)?(?:"
        r"try again\b.{0,160}|retry\b.{0,160}|"
        r"please (?:wait|try again|retry)\b.{0,160}|"
        r"available\b.{0,160}|after\b.{0,160}|in \d+\b.{0,160}))?\s*",
        re.I,
    ),
}


def _classify_result_refusal(text: str) -> q.Failure | None:
    """Classify only stdout bodies that look like a CLI refusal.

    ``q.classify`` is intentionally broad for stderr and failed turns. A
    successful answer may legitimately discuss HTTP 401/429/500 or timeouts,
    so result-text fallback uses anchored vendor-style refusal phrases and
    never infers a transient failure from an otherwise successful answer.
    """
    failure = q.classify(text)
    if failure is None or failure.kind not in _RESULT_REFUSALS:
        return None
    compact = " ".join(text.strip().split())
    return failure if _RESULT_REFUSALS[failure.kind].fullmatch(compact) else None


class State(str, Enum):
    OK = "ok"
    COOLING = "cooling"        # it told us it is out - wait for the reset
    TRIPPED = "tripped"        # it keeps breaking - back off exponentially
    HALF_OPEN = "half_open"    # one probe turn allowed


@dataclass
class Health:
    key: str
    state: State = State.OK
    until: float = 0.0
    consecutive: int = 0
    last_error: str = ""
    last_ok: float = 0.0
    ewma_seconds: float = 0.0
    quota: q.Quota | None = None
    quota_checked: float = 0.0
    quota_observation_epoch: int = 0

    def refresh(self) -> None:
        """Cooldowns expire on their own; a tripped breaker goes half-open."""
        if self.state in (State.COOLING, State.TRIPPED) and time.time() >= self.until:
            self.state = State.HALF_OPEN if self.state is State.TRIPPED else State.OK
            if self.state is State.OK:
                self.consecutive = 0

    @property
    def usable(self) -> bool:
        self.refresh()
        return self.state in (State.OK, State.HALF_OPEN)

    def describe(self) -> str:
        self.refresh()
        if self.state is State.OK:
            return "ok"
        wait = max(0.0, self.until - time.time())
        unit = f"{wait / 3600:.1f}h" if wait > 5400 else f"{wait / 60:.0f}m"
        return f"{self.state.value} for {unit}"


@dataclass
class Attempt:
    """One backend tried inside a request. Used by `--print --json`."""
    key: str
    label: str
    ok: bool = False
    error: str = ""
    failure_kind: str = ""
    continuation_guard: bool = False
    seconds: float = 0.0
    timed_out: bool = False

    def as_dict(self) -> dict:
        return {
            "agent": self.key,
            "exit_code": 0 if self.ok else (124 if self.timed_out else 1),
            "quota_error": self.failure_kind == "quota",
            "timed_out": self.timed_out,
            "duration_ms": int(round(self.seconds * 1000)),
            "continuation_guard": self.continuation_guard,
            "error": self.error,
        }


@dataclass
class Decision:
    """What the router did, for `/quota` and for the debug line in chat."""
    chosen: AgentSpec | None
    tried: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)

    def describe(self) -> str:
        if len(self.tried) <= 1:
            return ""
        return " -> ".join(self.tried) + (
            f"  ({'; '.join(self.reasons)})" if self.reasons else "")


class Router:
    def __init__(self, config: Config, pool: AgentPool) -> None:
        self.config = config
        self.pool = pool
        self.health: dict[str, Health] = {
            a.key: Health(key=a.key) for a in config.agents
        }
        self.ledger = q.Ledger(Path(config.data_dir) / "ledger.json")
        self.last_success: str | None = None
        # Process-local provenance for conversation continuity. Disk history
        # cannot prove that an arbitrary live runner owns this coding thread.
        self.conversation_success: str | None = None
        self.last_scores: dict = {}

    # -- health ---------------------------------------------------------------

    def h(self, spec: AgentSpec) -> Health:
        return self.health.setdefault(spec.key, Health(key=spec.key))

    def _cool(self, spec: AgentSpec, failure: q.Failure) -> None:
        health = self.h(spec)
        r = self.config.routing
        if failure.kind == "quota":
            # It told us when it comes back. Trust that, with a floor so we do
            # not hammer an agent whose message had no timestamp - or whose
            # timestamp has already gone by, which means we misread it.
            named = failure.resets_at
            if named is not None and named <= time.time():
                named = None
            until = named or (time.time() + r.quota_blind_cooldown)
            health.state, health.until = State.COOLING, until
            health.consecutive = 0
        elif failure.kind == "auth":
            health.state = State.COOLING
            health.until = time.time() + r.auth_cooldown
        else:
            health.consecutive += 1
            if health.consecutive >= r.trip_after:
                backoff = min(r.max_cooldown,
                              r.base_cooldown * 2 ** (health.consecutive - r.trip_after))
                health.state, health.until = State.TRIPPED, time.time() + backoff
        health.last_error = failure.detail or failure.kind
        # Fold the refusal into the quota view so `/quota` shows why.
        if failure.kind == "quota":
            health.quota_observation_epoch += 1
            health.quota = q.Quota(
                agent=spec.key,
                windows=[q.Window(name=failure.window or "limit", used_percent=100.0,
                                  resets_at=failure.resets_at, source=q.OBSERVED,
                                  detail=failure.detail[:80])],
            )
            health.quota_checked = time.time()

    def observe(self, spec: AgentSpec, turn: Turn) -> q.Failure | None:
        """Update health and the ledger from a completed turn."""
        health = self.h(spec)
        if (turn.meta.get("shutdown_interrupted")
                or turn.meta.get("cancelled")):
            # Cancellation belongs to the request lifecycle, not backend
            # health. Keep it terminal without tripping or charging the agent.
            return q.Failure(
                "transient", detail=turn.error or "request cancelled")
        if turn.meta.get("timeout_kind") == "sink":
            # Delivery failed after the backend produced an event. Do not replay
            # the task or punish the model for a stuck UI/network sink.
            return q.Failure("transient", detail=turn.error or "event sink timeout")
        self.ledger.record(spec.key, turn.seconds, turn.ok)
        health.ewma_seconds = (turn.seconds if not health.ewma_seconds
                               else 0.7 * health.ewma_seconds + 0.3 * turn.seconds)

        failure = q.classify(turn.error)
        if failure is None and turn.meta.get("is_error"):
            failure = q.classify(turn.text) or q.Failure("transient", detail=turn.short(120))
        if failure is None and not turn.ok:
            failure = q.classify(turn.text) or q.Failure("transient", detail=turn.short(120))
        if failure is None and turn.ok and len(turn.text) < 300:
            # Claude Code returns "You've hit your weekly limit" as the result
            # body, not as an error. Successful answers need a stricter check
            # than stderr so ordinary discussion of status codes is preserved.
            failure = _classify_result_refusal(turn.text)

        if failure is not None:
            self._cool(spec, failure)
            log.info("%s: %s (%s)", spec.label, failure.kind, self.h(spec).describe())
            return failure

        health.state, health.consecutive, health.last_ok = State.OK, 0, time.time()
        health.until = 0.0
        self.last_success = spec.key
        return None

    # -- quota ----------------------------------------------------------------

    @staticmethod
    def _probe_remaining(deadline: float) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("quota probe deadline expired")
        return remaining

    async def _probe_quota(self, spec: AgentSpec, deadline: float) -> q.Quota | None:
        if spec.quota_probe == "codex":
            remaining = self._probe_remaining(deadline)
            return await _run_sync_probe(
                q.probe_codex, None, self.config.sub2api, remaining,
                timeout=remaining)
        if spec.quota_probe == "grok":
            runner = self.pool.peek(spec)
            conn = getattr(runner, "_conn", None)
            remaining = self._probe_remaining(deadline)
            found = await q.probe_grok_acp(
                conn, timeout=min(q.GROK_ACP_PROBE_TIMEOUT, remaining))
            if found is None:
                remaining = self._probe_remaining(deadline)
                found = await _run_sync_probe(
                    q.probe_grok_rest, None, remaining,
                    timeout=remaining)
            if found is None:
                found = await _run_sync_probe(
                    q.probe_grok_local,
                    timeout=self._probe_remaining(deadline))
            return found
        if spec.quota_probe == "claude":
            return await _run_sync_probe(
                q.probe_claude, timeout=self._probe_remaining(deadline))
        if spec.quota_probe == "cursor":
            return await _run_sync_probe(
                q.probe_cursor, timeout=self._probe_remaining(deadline))
        return None

    @staticmethod
    def _cached_real_quota(spec: AgentSpec, previous: q.Quota | None) -> q.Quota:
        """Keep a stale reported/observed snapshot when a live probe is silent."""
        if previous is None:
            return q.Quota(agent=spec.key)
        windows = [
            window for window in previous.windows
            if window.source in (q.REPORTED, q.OBSERVED) and not window.expired
        ]
        return q.Quota(
            agent=spec.key,
            windows=windows,
            checked_at=previous.checked_at,
            note=previous.note if windows else "",
            title=previous.title if windows else "",
            products=list(previous.products) if windows else [],
            extras=dict(previous.extras) if windows else {},
        )

    async def quota_for(self, spec: AgentSpec, force: bool = False) -> q.Quota:
        health = self.h(spec)
        ttl = self.config.routing.quota_ttl
        if not force and health.quota and time.time() - health.quota_checked < ttl:
            return health.quota

        previous = health.quota
        observation_epoch = health.quota_observation_epoch
        found: q.Quota | None = None
        try:
            budget = max(0.0, float(self.config.routing.quota_probe_timeout))
            deadline = asyncio.get_running_loop().time() + budget
            found = await self._probe_quota(spec, deadline)
        except Exception as exc:                      # noqa: BLE001
            log.debug("quota probe failed for %s: %s", spec.key, exc)

        cached = self._cached_real_quota(spec, previous)
        found_real = bool(found and any(
            w.source in (q.REPORTED, q.OBSERVED) and not w.expired
            for w in found.windows
        ))
        # A silent or empty refresh must not wipe a live reported window.
        # Ranking keeps that last real number. leftover quota / doctor
        # (force=True) may attach a live failure note on top of it.
        result = found if found_real or not cached.windows else cached
        if result is None:
            result = cached
        if (force and found is not None and found.note and not found_real
                and result is not found):
            result = q.Quota(
                agent=result.agent,
                windows=list(result.windows),
                checked_at=result.checked_at,
                note=found.note,
                title=result.title,
                products=list(result.products),
                extras=dict(result.extras),
            )
        # Keep refusals observed while this probe was in flight. Router health
        # is shared across conversations, so observe() may have replaced the
        # cached quota after `previous` was captured above.
        concurrent_quota = (health.quota
                            if health.quota_observation_epoch != observation_epoch
                            else None)
        if concurrent_quota is not None:
            existing = {
                (w.name, w.source, w.resets_at, w.detail)
                for w in result.windows
            }
            for window in concurrent_quota.windows:
                identity = (window.name, window.source,
                            window.resets_at, window.detail)
                if (window.source == q.OBSERVED and not window.expired
                        and identity not in existing):
                    result.windows.append(window)
                    existing.add(identity)
        result.agent = spec.key
        # Ranking uses a turn budget when the vendor is silent. /quota now
        # draws those windows as "estimated local", not as vendor remaining.
        real = [w for w in result.windows
                if w.source in (q.REPORTED, q.OBSERVED) and not w.expired]
        if not real:
            result.windows += self.ledger.budget_windows(
                spec.key, spec.budget_5h_turns, spec.budget_week_turns)
        health.quota, health.quota_checked = result, time.time()
        result.checked_at = health.quota_checked
        return result

    # -- ranking --------------------------------------------------------------

    def _priority(self, spec: AgentSpec) -> int:
        if self.config.routing.strategy == "lag_waste":
            keys = self.config.routing.coding_keys
            if spec.key in keys:
                return keys.index(spec.key)
        order = self.config.routing.order
        return order.index(spec.key) if spec.key in order else len(order)

    async def rank(self, specs: list[AgentSpec]) -> list[AgentSpec]:
        strategy = self.config.routing.strategy
        usable = [s for s in specs if self.h(s).usable]
        blocked = [s for s in specs if not self.h(s).usable]

        if strategy == "order":
            ranked = sorted(usable, key=self._priority)
        elif strategy == "cheapest":
            ranked = sorted(usable, key=lambda s: (s.tier != "light",
                                                   self._priority(s)))
        elif strategy == "sticky":
            ranked = sorted(usable, key=lambda s: (s.key != self.last_success,
                                                   self._priority(s)))
        elif strategy == "lag_waste":
            quotas = await asyncio.gather(*(self.quota_for(s) for s in usable))
            from .score import score_quota
            r = self.config.routing
            scores = {
                s.key: score_quota(s.key, qq, lag_weight=r.lag_weight,
                                   waste_weight=r.waste_weight)
                for s, qq in zip(usable, quotas)
            }
            self.last_scores = scores
            ranked = sorted(usable, key=lambda s: (-scores[s.key].total,
                                                   self._priority(s)))
        else:                                          # headroom
            quotas = await asyncio.gather(*(self.quota_for(s) for s in usable))
            scores = {
                s.key: self._score(s, qq) for s, qq in zip(usable, quotas)
            }
            ranked = sorted(usable, key=lambda s: (-scores[s.key], self._priority(s)))

        # Blocked agents stay on the end: if everything is blocked we would
        # rather try the least-recently-blocked one than answer nothing.
        return ranked + sorted(blocked, key=lambda s: self.h(s).until)

    def _score(self, spec: AgentSpec, quota: q.Quota) -> float:
        r = self.config.routing
        score = r.headroom_weight * quota.headroom
        score += r.priority_weight * (1.0 - self._priority(spec) / max(len(self.config.agents), 1))
        if spec.tier == "light":
            score += r.cheap_bonus
        health = self.h(spec)
        if health.ewma_seconds:
            score -= r.latency_weight * min(1.0, health.ewma_seconds / 300.0)
        score -= r.failure_penalty * min(health.consecutive, 3)
        return score

    # -- the actual call ------------------------------------------------------

    def chain_for(self, primary: AgentSpec | None, pool_specs: list[AgentSpec],
                  exclude: set[str] | None = None) -> list[AgentSpec]:
        """Primary first, then its declared fallbacks, then everyone else."""
        blocked = exclude or set()
        seen: list[AgentSpec] = []

        def push(spec: AgentSpec | None) -> None:
            if spec is None or not spec.enabled or spec.key in blocked:
                return
            if spec not in seen:
                seen.append(spec)

        push(primary)
        if primary is not None:
            for key in primary.fallback:
                push(self.config.find(key))
        for spec in pool_specs:
            push(spec)
        return seen

    async def run(self, prompt_for, primary: AgentSpec | None = None,
                  sink=None, max_attempts: int | None = None,
                  exclude: set[str] | None = None,
                  ordered_chain: list[AgentSpec] | None = None,
                  on_failover=None,
                  ) -> tuple[Turn, Decision]:
        """Run one turn with automatic fallback.

        `prompt_for(spec)` builds the prompt for whichever agent ends up
        taking the turn, so each attempt gets its own persona and framing.
        `ordered_chain` pins a caller's routing decision without injecting
        per-agent fallback preferences or re-ranking it.
        `on_failover(src, dest, failure, guarded)` is called before a
        substitute starts — usher's stderr notice lives in the CLI, not here.
        """
        if ordered_chain is None:
            ranked = await self.rank(self.config.enabled_agents())
            chain = self.chain_for(primary, ranked, exclude)
        else:
            blocked = exclude or set()
            chain = []
            for spec in ordered_chain:
                if (spec.enabled and spec.key not in blocked
                        and spec not in chain):
                    chain.append(spec)
        limit = max_attempts or self.config.routing.max_attempts

        # Calling an agent we already know is benched just burns a request and
        # the user's time. If none of the candidates is usable, say so instead.
        usable = [s for s in chain if self.h(s).usable]
        decision = Decision(chosen=None)
        for spec in chain:
            if spec not in usable:
                decision.reasons.append(f"{spec.key} {self.h(spec).describe()}")
        if not usable:
            return self._nobody_home(chain, primary, decision), decision

        last: Turn | None = None
        last_failure: q.Failure | None = None
        last_spec: AgentSpec | None = None
        attempted = False

        for spec in usable[:limit]:
            guarded = attempted and self.config.routing.continuation_guard
            if attempted and on_failover is not None and last_spec is not None:
                notice = on_failover(last_spec, spec, last_failure, guarded)
                if asyncio.iscoroutine(notice):
                    await notice
            decision.tried.append(spec.key)
            on_event: OnEvent | None = None
            if sink is not None:
                sink_started = time.monotonic()
                try:
                    on_event = await await_bounded(
                        sink(spec), self.config.routing.event_sink_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - delivery is advisory
                    elapsed = time.monotonic() - sink_started
                    if isinstance(exc, TimeoutError):
                        detail = ("event sink timed out after "
                                  f"{self.config.routing.event_sink_timeout:g}s")
                    else:
                        detail = f"event sink failed: {type(exc).__name__}: {exc}"
                    turn = Turn(
                        agent=spec, error=detail, seconds=elapsed,
                        meta={"timeout_kind": "sink"})
                    self.observe(spec, turn)
                    decision.reasons.append(f"{spec.key}: sink")
                    decision.attempts.append(Attempt(
                        key=spec.key, label=spec.label, error=detail,
                        failure_kind="sink", continuation_guard=guarded,
                        seconds=elapsed, timed_out=True))
                    return turn, decision
            prompt = prompt_for(spec)
            if guarded:
                prompt = CONTINUATION_GUARD + prompt
            turn = await self.pool.run(spec, prompt, on_event)
            attempted = True
            failure = self.observe(spec, turn)
            timed_out = self._terminal_timeout(turn)
            terminal = self._terminal_turn(turn)
            last, last_failure, last_spec = turn, failure, spec
            decision.attempts.append(Attempt(
                key=spec.key,
                label=spec.label,
                ok=failure is None and turn.ok,
                error=(turn.error or (failure.detail if failure else "")
                       or "")[:200],
                failure_kind="" if failure is None else failure.kind,
                continuation_guard=guarded,
                seconds=turn.seconds,
                timed_out=timed_out,
            ))
            if failure is None and turn.ok:
                decision.chosen = spec
                return turn, decision
            decision.reasons.append(
                f"{spec.key}: {failure.kind if failure else 'empty'}")
            if terminal:
                # A configured turn/idle deadline means this was real work that
                # ran too long. Starting the same job on another vendor doubles
                # latency and cost. A pool shutdown boundary likewise means this
                # routed request has been cancelled and must not restart later.
                # Quick refusals and queue saturation still use normal fallback.
                return turn, decision
        if last is None:
            return self._nobody_home(chain, primary, decision), decision
        if last.error is None and last_failure is not None:
            # A limit message is text, not an exception - Claude Code returns
            # "You've hit your weekly limit" as an ordinary result. Mark it as
            # the failure it is so callers never print it as an answer.
            last.error = f"{last_failure.kind}: {last_failure.detail[:120]}"
        return last, decision

    @staticmethod
    def _terminal_timeout(turn: Turn) -> bool:
        if turn.meta.get("shutdown_interrupted"):
            return True
        if turn.meta.get("timeout_kind") in {"turn", "idle", "sink"}:
            return True
        error = (turn.error or "").strip().lower()
        return (error.startswith("timed out after ")
                or error.startswith("acp idle timed out after "))

    def _terminal_turn(self, turn: Turn) -> bool:
        """True when retrying this completed request would duplicate work."""
        return self._terminal_timeout(turn) or bool(
            turn.meta.get("cancelled"))

    def _nobody_home(self, chain: list[AgentSpec], primary: AgentSpec | None,
                     decision: Decision) -> Turn:
        """Every candidate is benched - report when the first one is back."""
        waits = [(self.h(s).until, s) for s in chain if self.h(s).until]
        who = chain[0] if chain else (primary or NOBODY)
        if waits:
            until, soonest = min(waits, key=lambda pair: pair[0])
            mins = max(0.0, until - time.time()) / 60
            when = f"{mins / 60:.1f}h" if mins > 90 else f"{mins:.0f}m"
            return Turn(agent=who,
                        error=f"every agent is benched; {soonest.label} "
                              f"is back in about {when}")
        return Turn(agent=who, error="no agent available")

    # -- reporting ------------------------------------------------------------

    async def snapshot(self) -> list[tuple[AgentSpec, q.Quota, q.Quota | None]]:
        specs = [spec for spec in self.config.agents if spec.enabled]
        previous = [self.h(spec).quota for spec in specs]
        quotas = await asyncio.gather(*(
            self.quota_for(spec, force=True) for spec in specs
        ))
        return list(zip(specs, quotas, previous))

    async def report(self) -> str:
        from . import rhythm
        now = time.time()
        return rhythm.render(
            await self.snapshot(), now=now,
            strategy=self.config.routing.strategy,
            order=self.config.routing.order,
            tz_name=self.config.timezone)

    async def report_payload(self) -> dict[str, Any]:
        from . import rhythm
        now = time.time()
        return rhythm.payload(
            await self.snapshot(), now=now,
            strategy=self.config.routing.strategy,
            order=self.config.routing.order,
            tz_name=self.config.timezone)
