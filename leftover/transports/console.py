"""Terminal front end - same orchestrator, no Telegram token needed.

Useful for checking that your CLIs actually answer before wiring up the bot.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from .. import ui
from ..agents import AgentPool, Event
from ..config import AgentSpec, Config
from ..orchestrator import Orchestrator, summarise

BANNER = """agora console. @claude / @gpt / @grok / @cursor to address one,
@any to let the router choose. /rt /all /debate /relay for group modes.
/quota /cd /who /reset /quit."""


def _sink_factory(stream=sys.stdout):
    async def sink(spec: AgentSpec):
        stream.write(f"\n{spec.emoji} {spec.label}: ")
        stream.flush()

        async def on_event(ev: Event) -> None:
            if ev.kind == "text":
                stream.write(ev.text)
            elif ev.kind == "tool":
                stream.write(f"\n  [{ev.text}]\n  ")
            elif ev.kind == "status":
                stream.write(f"\n  {ev.text}\n  ")
            elif ev.kind == "error":
                stream.write(f"\n  !! {ev.text}\n")
            elif ev.kind == "done":
                stream.write("\n")
            stream.flush()

        return on_event

    sink.leftover_turn_status = True  # type: ignore[attr-defined]
    return sink


async def _repl(config: Config) -> None:
    pool = AgentPool(config)
    orch = Orchestrator(config, pool)
    sink = _sink_factory()
    print(BANNER)
    print(f"workdir: {pool.workdir}\n")
    loop = asyncio.get_running_loop()

    try:
        while True:
            try:
                line = (await loop.run_in_executor(None, input, "you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if line == "/who":
                for a in config.agents:
                    print(f"  [{'on ' if a.installed else 'off'}] {a.label} "
                          f"@{a.key} ({a.tier})")
                continue
            if line == "/quota":
                print(await orch.router.report())
                continue
            if line == "/reset":
                orch.transcript.clear()
                print("  transcript cleared")
                continue
            if line.startswith("/cd"):
                target = Path(os.path.expanduser(line[3:].strip() or "~")).resolve()
                if target.is_dir():
                    await pool.set_workdir(str(target))
                    print(f"  workdir -> {target}")
                else:
                    print(f"  no such directory: {target}")
                continue

            plan = orch.parse(line, in_group=False)
            if plan is None or not plan.actionable:
                print("  nothing to do - try /help")
                continue
            progress = (None if plan.mode == "ask"
                        else ui.Roster(mode=plan.mode))
            turns = await orch.execute(plan, sink, progress=progress)
            note = orch.last_decision.describe() if orch.last_decision else ""
            print(f"\n[{plan.mode}] {summarise(turns)}"
                  + (f"\n  routed: {note}" if note else "") + "\n")
    finally:
        await pool.shutdown()


def main(config: Config) -> None:
    asyncio.run(_repl(config))
