"""Turns a chat message into one or more agent turns.

Modes
-----
ask         one named agent answers, seeing the shared transcript
broadcast   every agent answers the same question in parallel
roundtable  agents answer in sequence, each reading the previous answers
debate      two agents argue assigned sides for N rounds, a third judges
relay       plan -> implement -> review pipeline for actual heavy work
heavy       Grok-Heavy-shaped local collab: independent parallel takes,
            then parallel compare-notes (workers discuss, leader synthesizes)
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Protocol, Sequence

from .agents import AgentPool, Event, Turn
from .config import AgentSpec, Config
from .router import CONTINUATION_GUARD, Decision, Router, await_bounded
from .transcript import Transcript

MENTION_RE = re.compile(r"(?:^|\s)@([A-Za-z][\w-]*)")
# "@any what do you think" - no preference, router picks.
AUTO_TOKENS = {"any", "auto", "best", "whoever"}

_GROUP_FRAME = (
    "You are {label}, a subagent leftover spawned for a multi-model discussion.\n"
    "The other subagents are: {others}.\n"
    "{persona}\n"
    "Address the human. leftover is routing, not your reader. Do not "
    "impersonate the others. Add something they did not say, or disagree "
    "with a reason. Keep it tight. As the task gets harder, compress "
    "harder: depth is a sharper claim, not a longer briefing. Implementing "
    "still needs the change and why."
)

_HEAVY_EXTRA = (
    "This is leftover heavy: Grok-Heavy-shaped local collab on this Mac.\n"
    "If the human is asking a question or weighing options: discuss. "
    "Tradeoffs first. Do not silently rewrite the repo.\n"
    "If the human is writing or building: make the smallest change that "
    "moves the work and explain it."
)

_HEAVY_INDEPENDENT = (
    "This is leftover heavy: Grok-Heavy-shaped local collab.\n"
    "You are working independently in parallel with the other subagents. "
    "You cannot see their answers yet; do not wait for them.\n"
    "If this is a question or decision: take a position. Tradeoffs first. "
    "Do not edit files.\n"
    "If this is writing or building: propose the smallest change that would "
    "move the work. Do not edit files yet — the leader implements after "
    "comparing notes."
)

_HEAVY_DISCUSS = (
    "Independent takes are in. This is the compare-notes round; everyone "
    "is writing in parallel from those takes, so do not wait for peers.\n"
    "Add what others missed or disagree with a reason. Do not repeat your "
    "independent take. Do not edit files. The leader is synthesizing the "
    "conclusion in parallel from the same independent takes."
)

_HEAVY_SYNTHESIS = (
    "You are the leader. Independent takes are in. The others are comparing "
    "notes in parallel from the same takes; do not wait for them.\n"
    "Compare the independent takes, resolve conflicts, and give the practical "
    "conclusion.\n"
    "If this is a question or decision: the recommendation and why.\n"
    "If this is writing or building: make the change in the working directory "
    "now."
)

_DEBATE_RULES = (
    "This is a read-only debate. Never edit files, implement changes, or run "
    "commands with side effects. Use only the proposition and statements "
    "supplied here unless the proposition explicitly asks you to inspect named "
    "repository files; in that case, use only read-only file or search tools "
    "needed for those named files. Keep your answer to at most 3 short points "
    "and 120 words."
)


@dataclass
class Plan:
    """What the orchestrator decided to do with an incoming message."""
    mode: str
    prompt: str
    agents: list[AgentSpec]
    meta: dict[str, str]
    # True when the user did not name anyone - the router picks.
    auto: bool = False

    @property
    def actionable(self) -> bool:
        return bool(self.prompt.strip()) and (self.auto or bool(self.agents))


# Called before an agent starts speaking; returns a sink for its stream.
TurnSink = Callable[[AgentSpec], Awaitable[Callable[[object], Awaitable[None]] | None]]


class GroupProgress(Protocol):
    """Transport-neutral lifecycle observer for a multi-agent run."""

    async def begin_phase(
            self, *, mode: str, title: str, index: int, total: int,
            seats: Sequence[tuple[AgentSpec, str]], parallel: bool) -> None: ...

    def sink(self, seat: AgentSpec, role: str = "") -> TurnSink: ...

    async def finish(
            self, seat: AgentSpec, turn: Turn, role: str = "") -> None: ...

    async def end_phase(self) -> None: ...

_CANCEL_DRAIN_SECONDS = 0.25
log = logging.getLogger("leftover.orchestrator")


@dataclass
class _DeliveryBudget:
    """Cumulative time allowed inside buffered delivery callbacks."""

    timeout: float
    spent: float = 0.0

    def remaining(self) -> float:
        return self.timeout - self.spent

    def consume(self, seconds: float) -> None:
        self.spent += max(0.0, seconds)


def _consume_future(future: asyncio.Future) -> None:
    """Retrieve a detached cleanup result so asyncio does not log it later."""
    try:
        future.result()
    except BaseException:
        pass


async def _cancel_and_drain(tasks: Sequence[asyncio.Task]) -> None:
    """Cancel child work without turning cancellation into another long wait."""
    if not tasks:
        return
    for task in tasks:
        if not task.done():
            task.cancel()
    waiter = asyncio.gather(*tasks, return_exceptions=True)
    done, _ = await asyncio.wait({waiter}, timeout=_CANCEL_DRAIN_SECONDS)
    if waiter not in done:
        waiter.cancel()
        waiter.add_done_callback(_consume_future)


class Orchestrator:
    def __init__(self, config: Config, pool: AgentPool,
                 router: Router | None = None) -> None:
        self.config = config
        self.pool = pool
        # Health and quota are per machine, not per chat, so the router is
        # shared across every conversation this process is handling.
        self.router = router or Router(config, pool)
        self.transcript = Transcript(keep=config.transcript_turns)
        self.last_decision: Decision | None = None

    # --- routing -------------------------------------------------------------

    def parse(self, text: str, in_group: bool = True) -> Plan | None:
        text = text.strip()
        if not text:
            return None

        if text.startswith("/"):
            cmd, _, rest = text.partition(" ")
            cmd = cmd.lstrip("/").split("@")[0].lower()
            rest = rest.strip()
            if cmd in ("rt", "roundtable"):
                return Plan("roundtable", rest, self._heavy_first(), {})
            if cmd in ("all", "broadcast", "ask"):
                return Plan("broadcast", rest, self.config.enabled_agents(), {})
            if cmd == "debate":
                names = MENTION_RE.findall(rest)
                prompt = MENTION_RE.sub(" ", rest).strip() if names else rest
                return Plan("debate", prompt, self.debate_panel(names or None), {
                    "rounds": str(self.config.debate_rounds)})
            if cmd in ("relay", "job", "build"):
                return Plan("relay", rest, self._heavy_first()[:3], {})
            if cmd in ("heavy", "discuss"):
                return Plan("heavy", rest, self.heavy_panel(), {
                    "extra": _HEAVY_EXTRA})
            return None

        tokens = MENTION_RE.findall(text)
        wants_auto = any(t.lower() in AUTO_TOKENS for t in tokens)
        mentioned = [
            spec for token in tokens
            if (spec := self.config.find(token)) is not None
        ]
        stripped = MENTION_RE.sub(" ", text).strip() if tokens else text

        if wants_auto and not mentioned:
            return Plan("ask", stripped or text, [], {}, auto=True)
        if mentioned:
            mode = "broadcast" if len(mentioned) > 1 else "ask"
            return Plan(mode, stripped or text, mentioned, {})

        if not in_group:
            # No name given: let the router decide who has the headroom.
            return Plan("ask", text, [], {}, auto=True)
        if self.config.auto_reply:
            return Plan("roundtable", text, self._heavy_first()[:2], {})
        return None

    def _heavy_first(self) -> list[AgentSpec]:
        heavy = self.config.enabled_agents("heavy")
        light = [a for a in self.config.enabled_agents() if a.tier != "heavy"]
        return heavy + light

    def discussion_panel(self, names: list[str] | None = None) -> list[AgentSpec]:
        """Installed heterogeneous subagents for a complex-task discussion."""
        if names:
            found: list[AgentSpec] = []
            for token in names:
                spec = self.config.find(token)
                if spec is not None and spec.installed and spec not in found:
                    found.append(spec)
            return found
        keys: list[str] = []
        plan = self.config.routing.plan_key
        if plan:
            keys.append(plan)
        keys.extend(self.config.routing.coding_keys)
        found = []
        seen: set[str] = set()
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            spec = self.config.find(key)
            if spec is not None and spec.installed:
                found.append(spec)
        return found

    def heavy_panel(self, names: list[str] | None = None) -> list[AgentSpec]:
        """Grok as leader, then the planner, then the coding pool."""
        if names:
            return self.discussion_panel(names)
        keys: list[str] = []
        for key in (self.config.routing.heavy_key, self.config.routing.plan_key,
                    *self.config.routing.coding_keys):
            if key and key not in keys:
                keys.append(key)
        found: list[AgentSpec] = []
        for key in keys:
            spec = self.config.find(key)
            if spec is not None and spec.installed and spec not in found:
                found.append(spec)
        return found

    def debate_panel(self, names: list[str] | None = None) -> list[AgentSpec]:
        """Two advocates plus a configurable low-latency default judge."""
        panel = self.discussion_panel(names)
        if names:
            return panel[:3]
        judge = self.config.find(self.config.debate_judge_key)
        if judge is not None and judge.installed:
            sides = [spec for spec in panel if spec.key != judge.key][:2]
            if len(sides) == 2:
                return [*sides, judge]
        return panel[:3]

    # --- prompt building -----------------------------------------------------

    def _frame(self, spec: AgentSpec, extra: str = "",
               include_persona: bool = True) -> str:
        others = ", ".join(
            a.label for a in self.config.enabled_agents() if a.key != spec.key
        ) or "no one else right now"
        frame = _GROUP_FRAME.format(
            label=spec.label, others=others,
            persona=spec.persona if include_persona else "")
        return f"{frame}\n{extra}".strip()

    def _compose(self, spec: AgentSpec, ask: str, extra: str = "",
                 floor: Sequence[Turn] = (), history: str | None = None,
                 include_persona: bool = True) -> str:
        parts = [self._frame(spec, extra, include_persona)]
        if history is None:
            history = self.transcript.render(limit=self.config.transcript_turns)
        if history:
            parts.append(f"--- conversation so far ---\n{history}")
        if floor:
            def label(turn: Turn) -> str:
                role = turn.meta.get("discussion_role")
                round_no = turn.meta.get("discussion_round")
                suffix = f" [{role} R{round_no}]" if role and round_no else (
                    f" [{role}]" if role else "")
                return turn.agent.label + suffix

            said = "\n\n".join(
                f"{label(t)}: {t.short()}" for t in floor if t.ok)
            if said:
                parts.append(f"--- already said this round ---\n{said}")
        parts.append(f"--- your turn ---\n{ask}")
        return "\n\n".join(parts)

    # --- execution -----------------------------------------------------------

    async def execute(
            self, plan: Plan, sink: TurnSink | None = None,
            progress: GroupProgress | None = None) -> list[Turn]:
        self.transcript.add("You", plan.prompt)
        runner = {
            "ask": self._run_sequence,
            "roundtable": self._run_sequence,
            "heavy": self._run_heavy,
            "broadcast": self._run_parallel,
            "debate": self._run_debate,
            "relay": self._run_relay,
        }[plan.mode]
        return await runner(plan, sink, progress)

    async def _speak(self, spec: AgentSpec | None, builder, sink: TurnSink | None,
                     exclude: Iterable[str] = (), attempts: int | None = None,
                     record: bool = True,
                     ordered_chain: list[AgentSpec] | None = None) -> Turn:
        """One slot in the conversation, with fallback if the agent refuses.

        `builder(spec)` is re-run for whoever actually takes the turn, so a
        substitute gets its own persona rather than inheriting the framing of
        the agent that dropped out.
        """
        turn, decision = await self.router.run(
            builder, primary=spec, sink=sink, max_attempts=attempts,
            exclude=set(exclude), ordered_chain=ordered_chain)
        self.last_decision = decision
        if turn.ok and record:
            self.transcript.add(turn.agent.label, turn.text)
        return turn

    async def _emit_turn(self, turn: Turn, sink: TurnSink | None,
                         budget: _DeliveryBudget | None = None) -> None:
        """Flush one buffered group answer without interleaving text."""
        if sink is None:
            return
        timeout = self.config.routing.event_sink_timeout
        delivery = budget or _DeliveryBudget(timeout)

        async def deliver(factory: Callable[[], Awaitable[object]]) -> object:
            remaining = delivery.remaining()
            if remaining <= 0:
                raise TimeoutError
            loop = asyncio.get_running_loop()
            started = loop.time()
            try:
                return await await_bounded(factory(), remaining)
            finally:
                delivery.consume(loop.time() - started)

        try:
            on_event = await deliver(lambda: sink(turn.agent))
            if on_event is None:
                return
            include_status = bool(
                getattr(sink, "leftover_turn_status", False)
                or getattr(on_event, "leftover_turn_status", False))
            caption = self._turn_status(
                role=turn.meta.get("discussion_role", ""),
                round_no=turn.meta.get("discussion_round"),
                seconds=turn.seconds,
                tools=len(turn.tools),
            )
            if caption and include_status:
                await deliver(lambda: on_event(Event("status", caption)))
            for tool in turn.tools:
                await deliver(lambda tool=tool: on_event(Event("tool", tool)))
            if turn.text:
                await deliver(lambda: on_event(Event("text", turn.text)))
            if turn.error:
                await deliver(lambda: on_event(Event("error", turn.error)))
            await deliver(lambda: on_event(Event("done")))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - do not strand group work
            detail = (f"timed out after {timeout:g}s"
                      if isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                      else f"{type(exc).__name__}: {exc}")
            turn.meta = dict(turn.meta)
            turn.meta["delivery_error"] = detail
            log.warning("%s event delivery failed: %s", turn.agent.label, detail)

    @staticmethod
    def _turn_status(*, role: object = "", round_no: object = None,
                     seconds: float = 0.0, tools: int = 0) -> str:
        context: list[str] = []
        shown_role = str(role or "").strip().lower().replace("_", " ")
        if shown_role:
            context.append(shown_role)
        if round_no:
            context.append(f"round {round_no}")
        if seconds:
            context.append(f"{seconds:.0f}s")
        if tools:
            context.append(f"{tools} tool" + ("s" if tools != 1 else ""))
        return " · ".join(context)

    def _live_status_sink(
            self, sink: TurnSink | None, *, role: str,
            round_no: int | None = None) -> TurnSink | None:
        """Add one final caption to an explicitly marked live answer sink."""
        if sink is None:
            return None

        async def start(spec: AgentSpec):
            on_event = await sink(spec)
            if on_event is None:
                return None
            include_status = bool(
                getattr(sink, "leftover_turn_status", False)
                or getattr(on_event, "leftover_turn_status", False))
            if not include_status:
                return on_event
            loop = asyncio.get_running_loop()
            started = loop.time()
            tool_count = 0
            caption_sent = False

            async def contextual(ev) -> None:
                nonlocal caption_sent, tool_count
                kind = str(getattr(ev, "kind", "") or "")
                if kind == "tool" and getattr(ev, "text", ""):
                    tool_count += 1
                if kind == "done" and not caption_sent:
                    caption_sent = True
                    caption = self._turn_status(
                        role=role,
                        round_no=round_no,
                        seconds=max(0.0, loop.time() - started),
                        tools=tool_count,
                    )
                    if caption:
                        await on_event(Event("status", caption))
                await on_event(ev)

            if getattr(on_event, "leftover_lifecycle", False):
                contextual.leftover_lifecycle = True  # type: ignore[attr-defined]
            return contextual

        return start

    async def _run_sequence(
            self, plan: Plan, sink: TurnSink | None,
            progress: GroupProgress | None = None) -> list[Turn]:
        turns: list[Turn] = []
        attempts = None if plan.mode == "ask" else 2
        extra = plan.meta.get("extra", "")
        specs = list(plan.agents or [None])
        observed = progress is not None and plan.mode != "ask" \
            and all(spec is not None for spec in specs)
        roles = [f"speaker {index + 1}/{len(specs)}"
                 for index in range(len(specs))]
        if observed:
            await progress.begin_phase(
                mode=plan.mode, title="shared context", index=1, total=1,
                seats=[(spec, role) for spec, role in zip(specs, roles)
                       if spec is not None],
                parallel=False,
            )
        completed = False
        try:
            for index, spec in enumerate(specs):
                floor = list(turns)
                if observed and spec is not None:
                    live_sink = progress.sink(spec, roles[index])
                elif plan.mode != "ask":
                    live_sink = self._live_status_sink(
                        sink,
                        role=f"SPEAKER {index + 1}",
                        round_no=1,
                    )
                else:
                    live_sink = sink
                turn = await self._speak(
                    spec,
                    lambda s, f=floor, e=extra: self._compose(
                        s, plan.prompt, extra=e, floor=f),
                    live_sink,
                    exclude={t.agent.key for t in turns if t.agent},
                    attempts=attempts,
                )
                if plan.mode != "ask" and spec is not None:
                    turn.meta = dict(turn.meta)
                    turn.meta.update(
                        discussion_role=f"SPEAKER {index + 1}",
                        discussion_round=1,
                    )
                if observed and spec is not None:
                    await progress.finish(spec, turn, roles[index])
                turns.append(turn)
            completed = True
        finally:
            if observed:
                await progress.end_phase()
        if observed and completed:
            delivery = _DeliveryBudget(
                self.config.routing.event_sink_timeout)
            for turn in turns:
                await self._emit_turn(turn, sink, delivery)
        return turns

    async def _run_parallel(
            self, plan: Plan, sink: TurnSink | None,
            progress: GroupProgress | None = None) -> list[Turn]:
        sem = asyncio.Semaphore(max(1, int(self.config.max_parallel)))
        named = {s.key for s in plan.agents}
        claimed_spares: set[str] = set()
        fallback_lock = asyncio.Lock()
        delivery = _DeliveryBudget(self.config.routing.event_sink_timeout)
        rank_task: asyncio.Task[list[AgentSpec]] | None = None
        role = "member"
        if progress is not None:
            await progress.begin_phase(
                mode=plan.mode, title="independent answers",
                index=1, total=1,
                seats=[(spec, role) for spec in plan.agents],
                parallel=True,
            )

        async def ranked_agents() -> list[AgentSpec]:
            nonlocal rank_task
            if rank_task is None:
                rank_task = asyncio.create_task(
                    self.router.rank(self.config.enabled_agents()))
            return await asyncio.shield(rank_task)

        async def one(index: int, spec: AgentSpec) -> tuple[int, Turn]:
            live_sink = progress.sink(spec, role) if progress is not None else None
            async with sem:
                turn, decision = await self.router.run(
                    lambda s: self._compose(s, plan.prompt),
                    primary=spec, sink=live_sink, max_attempts=1,
                    ordered_chain=[spec])

            if not turn.ok and not self.router._terminal_turn(turn):
                ranked = await ranked_agents()
                guarded = bool(decision.tried) \
                    and self.config.routing.continuation_guard

                # Only spare ownership is serialized. Calls to distinct
                # replacements remain parallel under the shared semaphore.
                async with fallback_lock:
                    chain = self.router.chain_for(
                        spec, ranked, exclude=named | claimed_spares)
                    replacement = next(
                        (candidate for candidate in chain
                         if self.router.h(candidate).usable),
                        None,
                    )
                    if replacement is not None:
                        claimed_spares.add(replacement.key)

                if replacement is not None:
                    def fallback_prompt(replacement: AgentSpec) -> str:
                        prompt = self._compose(replacement, plan.prompt)
                        return CONTINUATION_GUARD + prompt if guarded else prompt

                    async with sem:
                        fallback_turn, fallback_decision = await self.router.run(
                            fallback_prompt,
                            primary=replacement,
                            sink=live_sink,
                            max_attempts=1,
                            ordered_chain=[replacement],
                        )
                    if guarded:
                        for attempt in fallback_decision.attempts:
                            attempt.continuation_guard = True
                    decision.tried.extend(fallback_decision.tried)
                    decision.reasons.extend(fallback_decision.reasons)
                    decision.attempts.extend(fallback_decision.attempts)
                    if fallback_decision.tried:
                        turn = fallback_turn
                        decision.chosen = fallback_decision.chosen

            self.last_decision = decision
            turn.meta = dict(turn.meta)
            turn.meta["discussion_role"] = "MEMBER"
            return index, turn

        slots: list[Turn | None] = [None] * len(plan.agents)
        completion_order: list[Turn] = []
        tasks = [
            asyncio.create_task(one(index, spec))
            for index, spec in enumerate(plan.agents)
        ]
        try:
            for completed in asyncio.as_completed(tasks):
                index, turn = await completed
                slots[index] = turn
                if progress is not None:
                    await progress.finish(plan.agents[index], turn, role)
                    completion_order.append(turn)
                else:
                    await self._emit_turn(turn, sink, delivery)
        except BaseException:
            cleanup = list(tasks)
            if rank_task is not None:
                cleanup.append(rank_task)
            await _cancel_and_drain(cleanup)
            raise
        finally:
            if progress is not None:
                await progress.end_phase()

        if progress is not None:
            for turn in completion_order:
                await self._emit_turn(turn, sink, delivery)

        turns = [turn for turn in slots if turn is not None]
        for t in turns:
            if t.ok:
                self.transcript.add(t.agent.label, t.text)
        return turns

    async def _run_debate(
            self, plan: Plan, sink: TurnSink | None,
            progress: GroupProgress | None = None) -> list[Turn]:
        if len(plan.agents) < 2:
            return await self._run_sequence(plan, sink, progress)
        pro, con, *rest = plan.agents
        judge = rest[0] if rest else None
        rounds = max(1, min(int(plan.meta.get(
            "rounds", str(self.config.debate_rounds))), 4))
        timeout = self.config.debate_turn_timeout
        turns: list[Turn] = []
        originals = {"FOR": pro, "AGAINST": con}
        reserved = {agent.key for agent in plan.agents}
        assigned: dict[str, AgentSpec] = {}
        history = self.transcript.render(
            exclude_last=True, limit=self.config.transcript_turns)

        warm = getattr(self.pool, "prepare", None)
        warm_tasks = [asyncio.create_task(warm(spec)) for spec in plan.agents] \
            if callable(warm) else []
        delivery = _DeliveryBudget(self.config.routing.event_sink_timeout)

        def builder(side: str, round_no: int, floor: list[Turn]):
            if round_no == 1:
                task = ("Present your strongest case and address the strongest "
                        "likely counterargument. The other side is writing in "
                        "parallel, so do not claim to have read its answer.")
            else:
                task = ("Rebut the opposing side using the prior-round statements "
                        "below. Do not repeat your opening argument.")
            extra = (f"You are arguing {side}. Round {round_no} of {rounds}. "
                     f"{task} {_DEBATE_RULES}")
            return lambda spec, e=extra, f=floor: self._compose(
                spec, plan.prompt, e, f, history=history,
                include_persona=False)

        async def invoke(
                spec: AgentSpec, prompt_builder,
                live_sink: TurnSink | None = None) -> Turn:
            try:
                return await await_bounded(
                    self._speak(
                        spec, prompt_builder, live_sink,
                        attempts=1, record=False,
                        ordered_chain=[spec]),
                    timeout,
                )
            except TimeoutError:
                turn = Turn(
                    agent=spec,
                    error=f"debate turn timed out after {timeout:g}s",
                    seconds=timeout,
                    meta={"timeout_kind": "turn"},
                )
                self.router.observe(spec, turn)
                return turn

        def spare(blocked: set[str], attempted: set[str]) -> AgentSpec | None:
            for spec in self.config.enabled_agents():
                if (spec.key not in blocked and spec.key not in attempted
                        and spec.installed and self.router.h(spec).usable):
                    return spec
            return None

        try:
            for i in range(rounds):
                round_no = i + 1
                floor = list(turns)
                specs = {
                    side: assigned.get(side, originals[side])
                    for side in ("FOR", "AGAINST")
                }
                sides = ("FOR", "AGAINST")
                round_reserved = reserved | {spec.key for spec in specs.values()}
                attempted_spares: set[str] = set()
                fallback_lock = asyncio.Lock()
                if progress is not None:
                    await progress.begin_phase(
                        mode=plan.mode, title="arguments",
                        index=round_no,
                        total=rounds + (1 if judge is not None else 0),
                        seats=[(specs[side], side.lower()) for side in sides],
                        parallel=True,
                    )

                async def resolve_side(index: int, side: str) \
                        -> tuple[int, str, Turn]:
                    prompt_builder = builder(side, round_no, floor)
                    seat = specs[side]
                    live_sink = (progress.sink(seat, side.lower())
                                 if progress is not None else None)
                    turn = await invoke(seat, prompt_builder, live_sink)
                    if not turn.ok and not self.router._terminal_turn(turn):
                        while (not turn.ok
                               and not self.router._terminal_turn(turn)):
                            async with fallback_lock:
                                replacement = spare(
                                    round_reserved, attempted_spares)
                                if replacement is not None:
                                    attempted_spares.add(replacement.key)
                            if replacement is None:
                                break
                            turn = await invoke(
                                replacement, prompt_builder, live_sink)
                    turn.meta = dict(turn.meta)
                    turn.meta.update(
                        discussion_role=side, discussion_round=round_no)
                    return index, side, turn

                round_turns: list[Turn | None] = [None, None]
                completion_order: list[Turn] = []
                round_tasks = [
                    asyncio.create_task(resolve_side(index, side))
                    for index, side in enumerate(sides)
                ]
                try:
                    for completed in asyncio.as_completed(round_tasks):
                        index, side, turn = await completed
                        round_turns[index] = turn
                        if turn.ok:
                            assigned[side] = turn.agent
                        if progress is not None:
                            await progress.finish(
                                specs[side], turn, side.lower())
                            completion_order.append(turn)
                        else:
                            await self._emit_turn(turn, sink, delivery)
                except BaseException:
                    await _cancel_and_drain(round_tasks)
                    raise
                finally:
                    if progress is not None:
                        await progress.end_phase()
                if progress is not None:
                    for turn in completion_order:
                        await self._emit_turn(turn, sink, delivery)
                turns.extend(turn for turn in round_turns if turn is not None)

            if judge is not None:
                floor = list(turns)
                extra = (
                    "You are the neutral judge. Compare the labeled FOR and "
                    "AGAINST statements, identify the decisive point, and give "
                    "the practical conclusion in at most 5 short lines and 120 "
                    f"words. {_DEBATE_RULES}"
                )
                def judge_builder(spec: AgentSpec, e=extra, f=floor) -> str:
                    return self._compose(
                        spec, plan.prompt, e, f, history=history,
                        include_persona=False)
                if progress is not None:
                    await progress.begin_phase(
                        mode=plan.mode, title="verdict",
                        index=rounds + 1, total=rounds + 1,
                        seats=[(judge, "judge")], parallel=False,
                    )
                live_sink = (progress.sink(judge, "judge")
                             if progress is not None else None)
                try:
                    verdict = await invoke(judge, judge_builder, live_sink)
                    if not verdict.ok:
                        blocked = reserved | {a.key for a in assigned.values()}
                        blocked.add(judge.key)
                        attempted_spares: set[str] = set()
                        while (not verdict.ok
                               and not self.router._terminal_turn(verdict)):
                            replacement = spare(blocked, attempted_spares)
                            if replacement is None:
                                break
                            attempted_spares.add(replacement.key)
                            verdict = await invoke(
                                replacement, judge_builder, live_sink)
                    verdict.meta = dict(verdict.meta)
                    verdict.meta.update(discussion_role="JUDGE")
                    turns.append(verdict)
                    if progress is not None:
                        await progress.finish(judge, verdict, "judge")
                finally:
                    if progress is not None:
                        await progress.end_phase()
                await self._emit_turn(verdict, sink, delivery)
        finally:
            await _cancel_and_drain(warm_tasks)

        for turn in turns:
            if not turn.ok:
                continue
            role = str(turn.meta.get("discussion_role", "debate")).lower()
            round_no = turn.meta.get("discussion_round")
            suffix = f" {role} R{round_no}" if round_no else f" {role}"
            self.transcript.add(f"{turn.agent.label} [{suffix.strip()}]", turn.text)
        return turns

    async def _run_heavy(
            self, plan: Plan, sink: TurnSink | None,
            progress: GroupProgress | None = None) -> list[Turn]:
        """Independent parallel takes, then parallel compare-notes.

        Workers discuss while the leader synthesizes from the independent
        takes. Neither round waits on a peer in the same round. Only the
        leader may edit the working directory, and only in synthesis.
        """
        if len(plan.agents) < 2:
            return await self._run_sequence(plan, sink, progress)

        seats = list(plan.agents)
        reserved = {agent.key for agent in plan.agents}
        history = self.transcript.render(
            exclude_last=True, limit=self.config.transcript_turns)
        warm = getattr(self.pool, "prepare", None)
        warm_tasks = [asyncio.create_task(warm(spec)) for spec in plan.agents] \
            if callable(warm) else []
        delivery = _DeliveryBudget(self.config.routing.event_sink_timeout)
        sem = asyncio.Semaphore(max(1, int(self.config.max_parallel)))
        leader_label = seats[0].label
        turns: list[Turn] = []

        def extra_for(role: str) -> str:
            if role == "LEADER":
                return (f"You are the leader ({leader_label}). "
                        f"{_HEAVY_INDEPENDENT}")
            if role == "WORKER":
                return (f"You are a worker. The leader is {leader_label}. "
                        f"{_HEAVY_INDEPENDENT}")
            if role == "DISCUSS":
                return f"The leader is {leader_label}. {_HEAVY_DISCUSS}"
            return _HEAVY_SYNTHESIS

        def builder(role: str, floor: list[Turn]):
            extra = extra_for(role)
            include_persona = role == "SYNTHESIS"
            return lambda spec, e=extra, f=floor, p=include_persona: (
                self._compose(
                    spec, plan.prompt, e, f, history=history,
                    include_persona=p))

        async def invoke(
                spec: AgentSpec, prompt_builder,
                live_sink: TurnSink | None = None) -> Turn:
            async with sem:
                return await self._speak(
                    spec, prompt_builder, live_sink,
                    attempts=1, record=False,
                    ordered_chain=[spec])

        def spare(blocked: set[str], attempted: set[str]) -> AgentSpec | None:
            for spec in self.config.enabled_agents():
                if (spec.key not in blocked and spec.key not in attempted
                        and spec.installed and self.router.h(spec).usable):
                    return spec
            return None

        async def parallel_round(
            entries: list[tuple[int, AgentSpec, str]],
            floor: list[Turn],
            round_no: int,
        ) -> list[Turn]:
            round_reserved = reserved | {spec.key for _, spec, _ in entries}
            attempted_spares: set[str] = set()
            fallback_lock = asyncio.Lock()

            async def resolve(index: int, seat: int, spec: AgentSpec,
                              role: str) -> tuple[int, int, Turn]:
                prompt_builder = builder(role, floor)
                live_sink = (progress.sink(spec, role.lower())
                             if progress is not None else None)
                turn = await invoke(spec, prompt_builder, live_sink)
                if not turn.ok and not self.router._terminal_turn(turn):
                    while (not turn.ok
                           and not self.router._terminal_turn(turn)):
                        async with fallback_lock:
                            replacement = spare(
                                round_reserved, attempted_spares)
                            if replacement is not None:
                                attempted_spares.add(replacement.key)
                        if replacement is None:
                            break
                        turn = await invoke(
                            replacement, prompt_builder, live_sink)
                turn.meta = dict(turn.meta)
                turn.meta.update(
                    discussion_role=role, discussion_round=round_no)
                return index, seat, turn

            slots: list[Turn | None] = [None] * len(entries)
            tasks = [
                asyncio.create_task(resolve(index, seat, spec, role))
                for index, (seat, spec, role) in enumerate(entries)
            ]
            try:
                for completed in asyncio.as_completed(tasks):
                    index, seat, turn = await completed
                    slots[index] = turn
                    original = entries[index][1]
                    role = entries[index][2]
                    if turn.ok:
                        seats[seat] = turn.agent
                        reserved.add(turn.agent.key)
                    if progress is not None:
                        await progress.finish(
                            original, turn, role.lower())
                    else:
                        await self._emit_turn(turn, sink, delivery)
            except BaseException:
                await _cancel_and_drain(tasks)
                raise
            return [turn for turn in slots if turn is not None]

        async def run_round(
            title: str,
            entries: list[tuple[int, AgentSpec, str]],
            floor: list[Turn],
            round_no: int,
        ) -> list[Turn]:
            if progress is not None:
                await progress.begin_phase(
                    mode=plan.mode, title=title,
                    index=round_no, total=2,
                    seats=[(spec, role.lower())
                           for _, spec, role in entries],
                    parallel=True,
                )
            round_turns: list[Turn] = []
            try:
                round_turns = await parallel_round(entries, floor, round_no)
            finally:
                if progress is not None:
                    await progress.end_phase()
            if progress is not None:
                for turn in round_turns:
                    await self._emit_turn(turn, sink, delivery)
            return round_turns

        try:
            independent = await run_round(
                "independent",
                [(i, spec, "LEADER" if i == 0 else "WORKER")
                 for i, spec in enumerate(seats)],
                [],
                1,
            )
            turns.extend(independent)
            if any(turn.ok for turn in independent):
                leader_label = seats[0].label
                compared = await run_round(
                    "compare-notes",
                    [(0, seats[0], "SYNTHESIS"),
                     *[(i, spec, "DISCUSS")
                       for i, spec in enumerate(seats) if i != 0]],
                    independent,
                    2,
                )
                turns.extend(compared)
        finally:
            await _cancel_and_drain(warm_tasks)

        for turn in turns:
            if not turn.ok:
                continue
            role = str(turn.meta.get("discussion_role", "heavy")).lower()
            round_no = turn.meta.get("discussion_round")
            suffix = f" {role} R{round_no}" if round_no else f" {role}"
            self.transcript.add(
                f"{turn.agent.label} [{suffix.strip()}]", turn.text)
        return turns

    async def _run_relay(
            self, plan: Plan, sink: TurnSink | None,
            progress: GroupProgress | None = None) -> list[Turn]:
        """Heavy-work pipeline: plan, implement, review."""
        stages = [
            ("Write a concrete implementation plan. List the files you would "
             "touch and what changes each needs. Do not write the code yet.",),
            ("Carry out the plan above in the working directory. Use your tools "
             "to actually read and edit files. Write to the human: what "
             "changed and what they should do next.",),
            ("Review the work above as a skeptical senior engineer. Verify it "
             "against the original request, run or inspect what you can, and "
             "list any real defects. If it is sound, say so plainly.",),
        ]
        if not plan.agents:
            return []
        cast = [plan.agents[i % len(plan.agents)] for i in range(len(stages))]
        roles = ("plan", "implement", "review")
        reserved = {agent.key for agent in cast}
        used: set[str] = set()
        turns: list[Turn] = []
        delivery = _DeliveryBudget(self.config.routing.event_sink_timeout)
        for index, (spec, (instruction,), role) in enumerate(
                zip(cast, stages, roles), start=1):
            floor = list(turns)
            if progress is not None:
                await progress.begin_phase(
                    mode=plan.mode, title=role,
                    index=index, total=len(stages),
                    seats=[(spec, role)], parallel=False,
                )
            live_sink = (
                progress.sink(spec, role)
                if progress is not None else self._live_status_sink(
                    sink, role=role.upper())
            )
            try:
                turn = await self._speak(
                    spec,
                    lambda s, i=instruction, f=floor: self._compose(
                        s, plan.prompt, i, f),
                    live_sink,
                    exclude=(reserved - {spec.key}) | used,
                    attempts=2,
                )
                turn.meta = dict(turn.meta)
                turn.meta.update(discussion_role=role.upper())
                if progress is not None:
                    await progress.finish(spec, turn, role)
            finally:
                if progress is not None:
                    await progress.end_phase()
            turns.append(turn)
            if progress is not None:
                await self._emit_turn(turn, sink, delivery)
            if turn.ok:
                used.add(turn.agent.key)
        return turns


def summarise(turns: Iterable[Turn]) -> str:
    lines = []
    for t in turns:
        delivery_error = t.meta.get("delivery_error")
        status = (f"delivery failed: {delivery_error}" if delivery_error
                  else "ok" if t.ok else (t.error or "empty"))
        role = t.meta.get("discussion_role")
        label = f"{t.agent.label}/{str(role).lower()}" if role else t.agent.label
        lines.append(f"{label}: {status} ({t.seconds:.0f}s)")
    return " | ".join(lines)
