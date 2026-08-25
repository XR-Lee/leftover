"""Parse a user message into a leftover job: plan, coding, or computer-use."""
from __future__ import annotations

import re
from dataclasses import dataclass

MENTION_RE = re.compile(r"(?:^|\s)@([A-Za-z][\w-]*)")

PLAN_PREFIXES = ("/plan",)
CU_PREFIXES = ("/cu", "/computer", "/computer-use")
HEAVY_PREFIXES = ("/heavy", "/discuss")
DISCUSS_PREFIXES = {
    "/rt": "roundtable",
    "/roundtable": "roundtable",
    "/all": "broadcast",
    "/debate": "debate",
    "/relay": "relay",
    "/heavy": "heavy",
    "/discuss": "heavy",
}
# Named only — automatic guessing of computer-use is out of v1.
CU_EXPLICIT = re.compile(
    r"\b(computer use|点界面|去点|codex app)\b", re.I)
# Named only — leftover does not infer "this feels like a discussion".
HEAVY_EXPLICIT = re.compile(
    r"(?:\bshould we\b|\bwhat if\b|\bdiscuss\b|"
    r"\blet'?s (?:write|draft|discuss)\b|"
    r"\bwrite together\b|\bdraft together\b|"
    r"该不该|怎么看|一起写|共同写|共同开|"
    r"[?？]|吗\s*$)",
    re.I,
)


@dataclass
class Intent:
    kind: str            # coding | plan | computer_use | heavy | roundtable | broadcast | debate | relay
    prompt: str
    named: str | None    # first @token
    raw: str
    named_all: list[str] | None = None

    @property
    def actionable(self) -> bool:
        return bool(self.prompt.strip()) or self.kind == "computer_use"


def parse(text: str) -> Intent:
    raw = text.strip()
    if not raw:
        return Intent("coding", "", None, raw)

    mentions = [t.lower() for t in MENTION_RE.findall(raw)]
    auto = {"any", "auto", "best", "whoever"}
    mentions = [m for m in mentions if m not in auto]
    named = mentions[0] if mentions else None

    kind = "coding"
    prompt = raw
    parts = raw.split(maxsplit=1)
    cmd = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    cmd_l = cmd.lower()

    if cmd_l in DISCUSS_PREFIXES:
        kind = DISCUSS_PREFIXES[cmd_l]
        prompt = rest.strip()
    elif cmd_l in CU_PREFIXES:
        kind = "computer_use"
        prompt = rest.strip()
    elif cmd_l in PLAN_PREFIXES:
        kind = "plan"
        prompt = rest.strip()
    else:
        prompt = raw

    if kind == "coding" and CU_EXPLICIT.search(raw):
        kind = "computer_use"
    if kind == "coding" and len(mentions) > 1:
        kind = "roundtable"
    if kind == "coding" and HEAVY_EXPLICIT.search(raw):
        kind = "heavy"

    if mentions:
        prompt = MENTION_RE.sub("", prompt).strip()

    return Intent(kind=kind, prompt=prompt, named=named, raw=raw,
                  named_all=mentions or None)
