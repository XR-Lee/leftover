"""End-to-end checks with mock agents - no subscriptions consumed."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leftover import render                                      # noqa: E402
from leftover.agents import AgentPool, Event, Turn               # noqa: E402
from leftover.config import AgentSpec, Config, Routing           # noqa: E402
from leftover.orchestrator import Orchestrator, Plan             # noqa: E402
from leftover.router import Router                               # noqa: E402

MOCK = str(ROOT / "tests" / "mock_acp_agent.py")
FAKE = str(ROOT / "tests" / "fake_cli.py")


def make_config() -> Config:
    def acp(key: str, label: str, tier: str) -> AgentSpec:
        return AgentSpec(key=key, label=label, emoji=label[0], tier=tier,
                         transport="acp", acp_command=[sys.executable, MOCK],
                         timeout=30, persona=f"You are {label}.")

    agents = [acp("claude", "Claude", "heavy"),
              acp("gpt", "Codex", "heavy"),
              acp("cursor", "Cursor", "heavy"),
              AgentSpec(key="grok", label="Grok", emoji="X", tier="light",
                        transport="exec", exec_command=[sys.executable, FAKE],
                        exec_output="json", exec_json_path="result",
                        aliases=["xai"], timeout=30)]
    return Config(agents=agents, max_parallel=4, transcript_turns=12,
                  data_dir=tempfile.mkdtemp(prefix="agora-test-"))


class CountingPool:
    """Delegate to a real pool while recording how many turns overlap.

    Wall-clock comparisons ("parallel is faster than serial") pass on a laptop
    and flake on a two-core CI runner, where scheduling overhead can exceed the
    mock latency being measured. Peak concurrency is the property these checks
    actually mean, and it is exact.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.active = 0
        self.max_active = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def reset(self) -> None:
        self.max_active = 0

    async def run(self, spec, prompt, on_event=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            return await self.inner.run(spec, prompt, on_event)
        finally:
            self.active -= 1


def check(label: str, cond: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    return cond


async def main() -> int:
    cfg = make_config()
    pool = CountingPool(AgentPool(cfg))
    orch = Orchestrator(cfg, pool)
    ok = True

    print("\n[1] routing")
    p = orch.parse("@claude what about the cache?", in_group=True)
    ok &= check("mention routes to one agent",
                p is not None and p.mode == "ask"
                and [a.key for a in p.agents] == ["claude"], str(p.prompt))
    p = orch.parse("@gpt @xai compare notes", in_group=True)
    ok &= check("two mentions -> broadcast",
                p is not None and p.mode == "broadcast"
                and [a.key for a in p.agents] == ["gpt", "grok"])
    ok &= check("group stays silent without a mention",
                orch.parse("just chatting", in_group=True) is None)
    p = orch.parse("just chatting", in_group=False)
    ok &= check("DM with no mention hands the pick to the router",
                p is not None and p.auto and not p.agents and p.actionable)
    p = orch.parse("/rt is rust worth it", in_group=True)
    ok &= check("/rt -> roundtable over everyone",
                p is not None and p.mode == "roundtable" and len(p.agents) == 4)
    p = orch.parse("/debate tabs beat spaces", in_group=True)
    ok &= check("/debate -> configured 3-agent panel",
                p is not None
                and [a.key for a in p.agents] == ["claude", "gpt", "cursor"])
    p = orch.parse(
        "/debate @cursor @claude @gpt tabs beat spaces", in_group=True)
    ok &= check("/debate preserves explicit panel order and strips mentions",
                p is not None
                and [a.key for a in p.agents] == ["cursor", "claude", "gpt"]
                and p.prompt == "tabs beat spaces")
    ok &= check("light agent sorts last in heavy-first order",
                orch._heavy_first()[-1].key == "grok")

    print("\n[2] single turn, streaming")
    events: list[str] = []

    async def sink(spec):
        async def on_event(ev):
            events.append(ev.kind)
        return on_event

    plan = orch.parse("@claude hello", in_group=True)
    turns = await orch.execute(plan, sink)
    ok &= check("agent answered", turns[0].ok, repr(turns[0].short(60)))
    ok &= check("streamed text + tool + done",
                {"text", "tool", "done"} <= set(events))
    ok &= check("transcript recorded both sides", len(orch.transcript) == 2)

    print("\n[3] roundtable sees prior answers")
    orch.transcript.clear()
    plan = orch.parse("/rt what breaks first", in_group=True)
    turns = await orch.execute(plan, None)
    ok &= check("all four spoke", len(turns) == 4 and all(t.ok for t in turns),
                " ".join(t.agent.key for t in turns))
    ok &= check("exec-transport agent worked",
                any(t.agent.key == "grok" and "exec reply" in t.text for t in turns))
    ok &= check("later agents were shown earlier answers",
                "already said this round" in orch._compose(
                    cfg.agents[1], "x", floor=turns[:1]))

    print("\n[4] broadcast runs in parallel")
    orch.transcript.clear()
    plan = orch.parse("/all quick take", in_group=True)
    pool.reset()
    turns = await orch.execute(plan, None)
    ok &= check("everyone answered", len(turns) == 4 and all(t.ok for t in turns))
    ok &= check("broadcast workers overlap instead of queueing",
                pool.max_active >= 2, f"peak {pool.max_active} in flight")

    print("\n[4b] buffered broadcast output stays grouped")

    class ChunkPool:
        async def run(self, spec, prompt, on_event=None):
            if on_event is not None:
                await on_event(Event("text", f"{spec.key}-early"))
            await asyncio.sleep(0.12 if spec.key == "one" else 0.01)
            if on_event is not None:
                await on_event(Event("text", f"{spec.key}-late"))
            return Turn(agent=spec, text=f"{spec.key}-answer",
                        tools=[f"{spec.key}-tool"])

    parallel_agents = [
        AgentSpec(key="one", label="One"),
        AgentSpec(key="two", label="Two"),
    ]
    parallel_cfg = Config(
        agents=parallel_agents,
        data_dir=tempfile.mkdtemp(prefix="agora-parallel-test-"),
    )
    parallel_pool = ChunkPool()
    parallel_orch = Orchestrator(
        parallel_cfg, parallel_pool, Router(parallel_cfg, parallel_pool))
    emitted: list[tuple[str, str, str]] = []
    first_emitted_after: list[float] = []
    emission_started = asyncio.get_running_loop().time()

    async def grouped_sink(spec):
        async def on_event(ev):
            if not emitted:
                first_emitted_after.append(
                    asyncio.get_running_loop().time() - emission_started)
            emitted.append((spec.key, ev.kind, ev.text))
        return on_event

    turns = await parallel_orch.execute(
        Plan("broadcast", "topic", parallel_agents, {}), grouped_sink)
    ok &= check("parallel work still completes for every slot",
                [turn.agent.key for turn in turns] == ["one", "two"])
    ok &= check("stdout-facing events are flushed one agent at a time",
                [key for key, _, _ in emitted]
                == ["two", "two", "two", "one", "one", "one"],
                repr(emitted))
    ok &= check("fast broadcast slot emits before the slow slot finishes",
                bool(first_emitted_after) and first_emitted_after[0] < 0.06,
                repr(first_emitted_after))
    ok &= check("buffered output retains tool visibility",
                [kind for _, kind, _ in emitted]
                == ["tool", "text", "done", "tool", "text", "done"],
                repr(emitted))

    print("\n[4c] broadcast fallback has exclusive spare ownership")

    class CollisionPool:
        def __init__(self):
            self.calls: list[str] = []
            self.active_primaries = 0
            self.max_active_primaries = 0
            self.active_spares = 0
            self.max_active_spares = 0
            self.both_primaries_started = asyncio.Event()

        async def run(self, spec, prompt, on_event=None):
            self.calls.append(spec.key)
            if spec.key in {"one", "two"}:
                self.active_primaries += 1
                self.max_active_primaries = max(
                    self.max_active_primaries, self.active_primaries)
                if self.active_primaries == 2:
                    self.both_primaries_started.set()
                try:
                    await asyncio.wait_for(
                        self.both_primaries_started.wait(), timeout=0.2)
                finally:
                    self.active_primaries -= 1
                return Turn(agent=spec, error="connection reset by peer")
            self.active_spares += 1
            self.max_active_spares = max(
                self.max_active_spares, self.active_spares)
            try:
                await asyncio.sleep(0.08)
            finally:
                self.active_spares -= 1
            return Turn(agent=spec, text=f"{spec.key} recovered")

    collision_agents = [
        AgentSpec(key=key, label=key.title())
        for key in ("one", "two", "spare-a", "spare-b")
    ]
    collision_cfg = Config(
        agents=collision_agents,
        data_dir=tempfile.mkdtemp(prefix="agora-broadcast-fallback-test-"),
    )
    collision_pool = CollisionPool()
    collision_orch = Orchestrator(
        collision_cfg, collision_pool,
        Router(collision_cfg, collision_pool),
    )
    collision_turns = await collision_orch.execute(
        Plan("broadcast", "topic", collision_agents[:2], {}), None)
    ok &= check("broadcast primaries still overlap",
                collision_pool.max_active_primaries == 2,
                repr(collision_pool.calls))
    ok &= check("each failed slot gets at most one distinct spare",
                len(collision_pool.calls) == 4
                and collision_pool.calls.count("spare-a") == 1
                and collision_pool.calls.count("spare-b") == 1
                and {turn.agent.key for turn in collision_turns}
                == {"spare-a", "spare-b"},
                repr(collision_pool.calls))
    ok &= check("distinct broadcast spares execute concurrently",
                collision_pool.max_active_spares == 2,
                f"calls={collision_pool.calls}")

    class BroadcastTimeoutPool:
        def __init__(self):
            self.calls: list[str] = []

        async def run(self, spec, prompt, on_event=None):
            self.calls.append(spec.key)
            if spec.key == "timed":
                return Turn(
                    agent=spec,
                    error="ACP idle timed out after 1s without an update",
                    meta={"timeout_kind": "idle"},
                )
            if spec.key == "retry":
                return Turn(agent=spec, error="connection reset by peer")
            return Turn(agent=spec, text="recovered")

    timeout_broadcast_agents = [
        AgentSpec(key=key, label=key.title())
        for key in ("timed", "retry", "spare")
    ]
    timeout_broadcast_cfg = Config(
        agents=timeout_broadcast_agents,
        data_dir=tempfile.mkdtemp(prefix="agora-broadcast-timeout-test-"),
    )
    timeout_broadcast_pool = BroadcastTimeoutPool()
    timeout_broadcast_orch = Orchestrator(
        timeout_broadcast_cfg,
        timeout_broadcast_pool,
        Router(timeout_broadcast_cfg, timeout_broadcast_pool),
    )
    timeout_broadcast_turns = await timeout_broadcast_orch.execute(
        Plan("broadcast", "topic", timeout_broadcast_agents[:2], {}), None)
    ok &= check("terminal broadcast timeout is not replayed",
                timeout_broadcast_pool.calls.count("spare") == 1
                and timeout_broadcast_turns[0].agent.key == "timed"
                and timeout_broadcast_turns[0].meta.get("timeout_kind") == "idle"
                and timeout_broadcast_turns[1].agent.key == "spare",
                repr(timeout_broadcast_pool.calls))

    print("\n[5] debate and relay shapes")
    orch.transcript.clear()
    plan = orch.parse("/debate monorepos are better", in_group=True)
    pool.reset()
    turns = await orch.execute(plan, None)
    ok &= check("1 parallel round x 2 sides + judge = 3 turns", len(turns) == 3,
                " ".join(t.agent.key for t in turns))
    ok &= check("debate sides overlap instead of adding their latencies",
                pool.max_active >= 2, f"peak {pool.max_active} in flight")
    ok &= check("debate records explicit roles",
                [t.meta.get("discussion_role") for t in turns]
                == ["FOR", "AGAINST", "JUDGE"])

    print("\n[5a] completed debate side emits without waiting for its peer")

    class DebateChunkPool:
        async def run(self, spec, prompt, on_event=None):
            await asyncio.sleep(0.12 if spec.key == "pro" else 0.01)
            return Turn(agent=spec, text=f"{spec.key} answer")

    debate_chunk_agents = [
        AgentSpec(key="pro", label="Pro"),
        AgentSpec(key="con", label="Con"),
    ]
    debate_chunk_cfg = Config(
        agents=debate_chunk_agents,
        data_dir=tempfile.mkdtemp(prefix="agora-debate-output-test-"),
        debate_turn_timeout=1,
    )
    debate_chunk_pool = DebateChunkPool()
    debate_chunk_orch = Orchestrator(
        debate_chunk_cfg,
        debate_chunk_pool,
        Router(debate_chunk_cfg, debate_chunk_pool),
    )
    debate_emitted: list[tuple[str, str]] = []
    debate_first_after: list[float] = []
    debate_started = asyncio.get_running_loop().time()

    async def debate_sink(spec):
        async def on_event(ev):
            if not debate_emitted:
                debate_first_after.append(
                    asyncio.get_running_loop().time() - debate_started)
            debate_emitted.append((spec.key, ev.kind))
        return on_event

    debate_chunk_turns = await debate_chunk_orch.execute(
        Plan("debate", "topic", debate_chunk_agents, {"rounds": "1"}),
        debate_sink,
    )
    ok &= check("debate return value keeps FOR/AGAINST slot order",
                [turn.agent.key for turn in debate_chunk_turns]
                == ["pro", "con"])
    ok &= check("fast debate slot emits before the slow side finishes",
                debate_emitted[:2] == [("con", "text"), ("con", "done")]
                and bool(debate_first_after) and debate_first_after[0] < 0.06,
                f"events={debate_emitted}, delay={debate_first_after}")

    print("\n[5b] debate timeout is not replayed on a spare")

    class TimeoutPool:
        def __init__(self):
            self.calls: list[str] = []

        async def run(self, spec, prompt, on_event=None):
            self.calls.append(spec.key)
            if spec.key == "pro":
                return Turn(
                    agent=spec,
                    error="ACP idle timed out after 1s without an update",
                    meta={"timeout_kind": "idle"},
                )
            return Turn(agent=spec, text=f"{spec.key} ok")

    debate_agents = [
        AgentSpec(key=key, label=key.title(),
                  interactive_command=[sys.executable])
        for key in ("pro", "con", "judge", "spare")
    ]
    timeout_cfg = Config(
        agents=debate_agents,
        data_dir=tempfile.mkdtemp(prefix="agora-timeout-test-"),
        debate_turn_timeout=1,
    )
    timeout_pool = TimeoutPool()
    timeout_orch = Orchestrator(
        timeout_cfg, timeout_pool, Router(timeout_cfg, timeout_pool))
    timeout_turns = await timeout_orch.execute(
        Plan("debate", "topic", debate_agents[:3], {"rounds": "1"}), None)
    ok &= check("timed-out advocate is reported without a spare replay",
                "spare" not in timeout_pool.calls
                and timeout_turns[0].meta.get("timeout_kind") == "idle",
                repr(timeout_pool.calls))

    print("\n[5c] debate fallbacks claim distinct spares concurrently")

    class DebateFallbackPool:
        def __init__(self):
            self.calls: list[str] = []
            self.active_spares = 0
            self.max_active_spares = 0

        async def run(self, spec, prompt, on_event=None):
            self.calls.append(spec.key)
            if spec.key in {"pro", "con"}:
                await asyncio.sleep(0.005 if spec.key == "pro" else 0.01)
                return Turn(agent=spec, error="connection reset by peer")
            self.active_spares += 1
            self.max_active_spares = max(
                self.max_active_spares, self.active_spares)
            try:
                await asyncio.sleep(0.08)
            finally:
                self.active_spares -= 1
            return Turn(agent=spec, text=f"{spec.key} recovered")

    debate_fallback_agents = [
        AgentSpec(key=key, label=key.title(),
                  interactive_command=[sys.executable])
        for key in ("pro", "con", "spare-a", "spare-b")
    ]
    debate_fallback_cfg = Config(
        agents=debate_fallback_agents,
        data_dir=tempfile.mkdtemp(prefix="agora-debate-fallback-test-"),
        debate_turn_timeout=1,
    )
    debate_fallback_pool = DebateFallbackPool()
    debate_fallback_orch = Orchestrator(
        debate_fallback_cfg,
        debate_fallback_pool,
        Router(debate_fallback_cfg, debate_fallback_pool),
    )
    debate_fallback_started = asyncio.get_running_loop().time()
    debate_fallback_turns = await debate_fallback_orch.execute(
        Plan("debate", "topic", debate_fallback_agents[:2], {"rounds": "1"}),
        None,
    )
    debate_fallback_elapsed = (
        asyncio.get_running_loop().time() - debate_fallback_started)
    ok &= check("each debate side owns a different spare",
                {turn.agent.key for turn in debate_fallback_turns}
                == {"spare-a", "spare-b"}
                and debate_fallback_pool.calls.count("spare-a") == 1
                and debate_fallback_pool.calls.count("spare-b") == 1,
                repr(debate_fallback_pool.calls))
    ok &= check("debate spare calls overlap instead of stacking timeouts",
                debate_fallback_pool.max_active_spares == 2,
                f"active={debate_fallback_pool.max_active_spares}, "
                f"elapsed={debate_fallback_elapsed:.3f}s")

    print("\n[5d] buffered output uses one group delivery budget")

    class FastGroupPool:
        async def run(self, spec, prompt, on_event=None):
            return Turn(agent=spec, text=f"{spec.key} answer")

    delivery_agents = [
        AgentSpec(key=f"delivery-{index}", label=f"Delivery {index}")
        for index in range(3)
    ]
    delivery_cfg = Config(
        agents=delivery_agents,
        data_dir=tempfile.mkdtemp(prefix="agora-delivery-budget-test-"),
        routing=Routing(
            strategy="order",
            order=[agent.key for agent in delivery_agents],
            event_sink_timeout=0.03,
        ),
    )
    delivery_pool = FastGroupPool()
    delivery_orch = Orchestrator(
        delivery_cfg, delivery_pool, Router(delivery_cfg, delivery_pool))
    delivery_factories: list[str] = []
    delivery_cancellations = 0

    async def blocked_group_sink(spec):
        delivery_factories.append(spec.key)

        async def on_event(_event):
            nonlocal delivery_cancellations
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                delivery_cancellations += 1
                raise

        return on_event

    delivery_started = asyncio.get_running_loop().time()
    delivery_turns = await delivery_orch.execute(
        Plan("broadcast", "topic", delivery_agents, {}), blocked_group_sink)
    delivery_elapsed = asyncio.get_running_loop().time() - delivery_started
    await asyncio.sleep(0)
    # One factory and one cancellation is the shared-budget contract: a
    # per-worker budget would have built a sink for every worker.
    ok &= check("bad group sinks consume one timeout total",
                len(delivery_factories) == 1
                and delivery_cancellations == 1,
                f"elapsed={delivery_elapsed:.3f}s, "
                f"factories={delivery_factories}, "
                f"cancellations={delivery_cancellations}")
    ok &= check("every undelivered answer keeps text and reports delivery error",
                all(turn.text and turn.ok
                    and turn.meta.get("delivery_error")
                    for turn in delivery_turns),
                repr([turn.meta for turn in delivery_turns]))

    class StaggeredGroupPool:
        async def run(self, spec, prompt, on_event=None):
            if spec.key == "delivery-slow":
                await asyncio.sleep(0.06)
            return Turn(agent=spec, text=f"{spec.key} answer")

    staggered_agents = [
        AgentSpec(key="delivery-fast", label="Delivery Fast"),
        AgentSpec(key="delivery-slow", label="Delivery Slow"),
    ]
    staggered_cfg = Config(
        agents=staggered_agents,
        data_dir=tempfile.mkdtemp(prefix="agora-delivery-gap-test-"),
        routing=Routing(
            strategy="order",
            order=[agent.key for agent in staggered_agents],
            event_sink_timeout=0.03,
        ),
    )
    staggered_pool = StaggeredGroupPool()
    staggered_orch = Orchestrator(
        staggered_cfg, staggered_pool, Router(staggered_cfg, staggered_pool))
    delivered: list[tuple[str, str]] = []

    async def instant_group_sink(spec):
        async def on_event(event):
            delivered.append((spec.key, event.kind))

        return on_event

    staggered_turns = await staggered_orch.execute(
        Plan("broadcast", "topic", staggered_agents, {}), instant_group_sink)
    ok &= check("model compute gaps do not consume the delivery budget",
                all("delivery_error" not in turn.meta
                    for turn in staggered_turns)
                and {(key, kind) for key, kind in delivered if kind == "done"}
                == {(agent.key, "done") for agent in staggered_agents},
                repr((delivered, [turn.meta for turn in staggered_turns])))

    print("\n[5e] debate warmup cannot outlive normal completion")

    class HangingWarmPool:
        def __init__(self):
            self.warm_started = 0
            self.warm_cancelled = 0

        async def prepare(self, spec):
            self.warm_started += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.warm_cancelled += 1
                raise

        async def run(self, spec, prompt, on_event=None):
            return Turn(agent=spec, text=f"{spec.key} ready")

    warm_agents = [
        AgentSpec(key="warm-pro", label="Warm Pro"),
        AgentSpec(key="warm-con", label="Warm Con"),
    ]
    warm_cfg = Config(
        agents=warm_agents,
        data_dir=tempfile.mkdtemp(prefix="agora-warmup-test-"),
        debate_turn_timeout=1,
    )
    warm_pool = HangingWarmPool()
    warm_orch = Orchestrator(
        warm_cfg, warm_pool, Router(warm_cfg, warm_pool))
    warm_started = asyncio.get_running_loop().time()
    warm_turns = await warm_orch.execute(
        Plan("debate", "topic", warm_agents, {"rounds": "1"}), None)
    warm_elapsed = asyncio.get_running_loop().time() - warm_started
    ok &= check("successful debate cancels speculative warmups promptly",
                len(warm_turns) == 2 and all(turn.ok for turn in warm_turns)
                and warm_pool.warm_started == 2
                and warm_pool.warm_cancelled == 2,
                f"started={warm_pool.warm_started}, "
                f"cancelled={warm_pool.warm_cancelled}, "
                f"elapsed={warm_elapsed:.3f}s")

    orch.transcript.clear()
    plan = orch.parse("/relay add a healthcheck endpoint", in_group=True)
    turns = await orch.execute(plan, None)
    ok &= check("relay ran plan/implement/review", len(turns) == 3)

    print("\n[6] rendering")
    long_md = ("intro\n\n```python\n" + "x = 1\n" * 900 + "```\n\ntail\n")
    parts = render.split(long_md, limit=1000)
    ok &= check("splits long text", len(parts) > 3, f"{len(parts)} parts")
    ok &= check("every part has balanced fences",
                all(p.count("```") % 2 == 0 for p in parts))
    html_out = render.to_html("**bold** and `code` <script>alert(1)</script>")
    ok &= check("escapes raw html", "&lt;script&gt;" in html_out)
    ok &= check("keeps bold and code tags",
                "<b>bold</b>" in html_out and "<code>code</code>" in html_out)
    fenced = render.to_html("see:\n```js\nlet a = 1 < 2;\n```\n")
    ok &= check("fences become pre/code",
                "<pre><code" in fenced and "1 &lt; 2" in fenced)

    print("\n[7] cancel + workdir switch")
    await pool.set_workdir(str(ROOT))
    ok &= check("workdir applied", pool.workdir == str(ROOT))
    turns = await orch.execute(orch.parse("@claude after cd", in_group=True), None)
    ok &= check("agents restart cleanly in the new dir", turns[0].ok)
    await pool.cancel_all()
    await pool.shutdown()
    ok &= check("shutdown is clean", True)

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
