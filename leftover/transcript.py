"""Shared conversation state for one chat."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Message:
    speaker: str            # "You" or an agent label
    text: str
    ts: float = field(default_factory=time.time)

    def render(self, limit: int = 1200) -> str:
        body = self.text.strip()
        if len(body) > limit:
            body = body[:limit].rstrip() + " ...[truncated]"
        return f"{self.speaker}: {body}"


class Transcript:
    def __init__(self, keep: int = 24) -> None:
        self.keep = keep
        self.messages: list[Message] = []

    def add(self, speaker: str, text: str) -> None:
        if text and text.strip():
            self.messages.append(Message(speaker, text.strip()))
            del self.messages[: max(0, len(self.messages) - self.keep)]

    def clear(self) -> None:
        self.messages.clear()

    def render(self, exclude_last: bool = False, limit: int | None = None) -> str:
        msgs = self.messages[:-1] if exclude_last else self.messages
        if limit:
            msgs = msgs[-limit:]
        return "\n\n".join(m.render() for m in msgs)

    def __len__(self) -> int:
        return len(self.messages)
