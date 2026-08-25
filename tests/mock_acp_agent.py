"""A tiny ACP agent used to test agora's runner without burning real quota.

Streams the prompt back word by word, emits one fake tool call, and honours
cancellation. Run it the way agora would: `python tests/mock_acp_agent.py`.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from acp import (
    PROTOCOL_VERSION,
    run_agent,
    start_tool_call,
    update_agent_message_text,
    update_tool_call,
)
from acp.interfaces import Agent
from acp.schema import (
    AgentCapabilities,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
)


class MockAgent(Agent):
    def __init__(self) -> None:
        self.conn: Any = None
        self.cancelled: set[str] = set()

    def on_connect(self, conn: Any) -> None:  # called synchronously by acp
        self.conn = conn

    async def initialize(self, protocol_version: int, client_capabilities: Any = None,
                         client_info: Any = None, **kwargs: Any) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=min(protocol_version, PROTOCOL_VERSION),
            agent_capabilities=AgentCapabilities(),
            agent_info=Implementation(name="mock-agent", version="0.1.0"),
        )

    async def new_session(self, cwd: str, additional_directories: Any = None,
                          mcp_servers: Any = None, **kwargs: Any) -> NewSessionResponse:
        return NewSessionResponse(session_id=f"mock-{uuid.uuid4().hex[:8]}")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self.cancelled.add(session_id)

    async def prompt(self, session_id: str, prompt: list[Any],
                     **kwargs: Any) -> PromptResponse:
        text = " ".join(getattr(b, "text", "") for b in prompt).strip()
        tag = text.splitlines()[-1][:60] if text else "(empty)"

        # MOCK_BEHAVIOR lets a test make this agent refuse in a specific way.
        behavior = os.environ.get("MOCK_BEHAVIOR", "ok")
        canned = {
            # Claude Code delivers limit messages as ordinary result text.
            "quota_weekly": "You've hit your weekly limit \u00b7 resets Mon 12:00am",
            "quota_session": "You've hit your session limit \u00b7 resets 3:45pm",
            "ratelimit": "API Error: Request rejected (429) \u00b7 temporary capacity issue.",
            "auth": "Please log in to continue - not authenticated.",
        }.get(behavior)
        if canned:
            await self.conn.session_update(
                session_id=session_id,
                update=update_agent_message_text(canned),
            )
            return PromptResponse(stop_reason="end_turn")
        if behavior == "crash":
            raise RuntimeError("connection reset by peer")
        if behavior == "slow":
            await asyncio.sleep(float(os.environ.get("MOCK_SLOW_SECONDS", "5")))
        if behavior == "long_tool":
            # Quiet in-flight execute, like pytest / cargo test. leftover must
            # not treat this silence as an ACP idle hang.
            seconds = float(os.environ.get("MOCK_TOOL_SECONDS", "0.2"))
            await self.conn.session_update(
                session_id=session_id,
                update=start_tool_call(
                    tool_call_id="pytest-1",
                    title="pytest",
                    kind="execute",
                    status="in_progress",
                    raw_input={"command": ["pytest", "-q"]},
                ),
            )
            await asyncio.sleep(seconds)
            if session_id in self.cancelled:
                self.cancelled.discard(session_id)
                return PromptResponse(stop_reason="cancelled")
            await self.conn.session_update(
                session_id=session_id,
                update=update_tool_call(
                    tool_call_id="pytest-1",
                    title="pytest",
                    status="completed",
                ),
            )
            await self.conn.session_update(
                session_id=session_id,
                update=update_agent_message_text("tests passed"),
            )
            return PromptResponse(stop_reason="end_turn")

        await self.conn.session_update(
            session_id=session_id,
            update=start_tool_call(tool_call_id="t1", title="read_file: notes.md"),
        )

        for word in f"mock reply to: {tag}".split():
            if session_id in self.cancelled:
                self.cancelled.discard(session_id)
                return PromptResponse(stop_reason="cancelled")
            await self.conn.session_update(
                session_id=session_id,
                update=update_agent_message_text(word + " "),
            )
            await asyncio.sleep(0.02)

        return PromptResponse(stop_reason="end_turn")


async def _serve() -> None:
    await run_agent(MockAgent())


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
