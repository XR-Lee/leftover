"""Configuration loading for leftover."""
from __future__ import annotations

import os
import shutil

try:                                  # 3.11+
    import tomllib
except ModuleNotFoundError:           # 3.10 and older need the backport
    try:
        import tomli as tomllib       # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None                # type: ignore[assignment]
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATHS = [
    Path.home() / ".config" / "leftover" / "leftover.toml",
    Path.home() / ".config" / "macbot" / "macbot.toml",
    Path.home() / ".config" / "agora" / "agora.toml",
    Path.cwd() / "leftover.toml",
    Path.cwd() / "macbot.toml",
    Path.cwd() / "agora.toml",
]
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "leftover"
LEGACY_DATA_DIR = Path.home() / ".local" / "share" / "agora"


@dataclass
class AgentSpec:
    """One AI agent backed by a locally-installed, already-logged-in CLI."""

    key: str                      # stable id, e.g. "claude"
    label: str                    # display name, e.g. "Claude"
    emoji: str = "*"
    aliases: list[str] = field(default_factory=list)
    enabled: bool = True

    # transport: "acp" (streaming, sessions) | "exec" (one-shot headless) | "auto"
    transport: str = "auto"
    acp_command: list[str] = field(default_factory=list)
    exec_command: list[str] = field(default_factory=list)
    # Interactive TUI argv (leftover hands the user to this). Empty = first
    # token of exec_command. Prompt is appended unless prompt_as_arg is false.
    interactive_command: list[str] = field(default_factory=list)
    prompt_as_arg: bool = True
    # where the prompt goes in exec_command: "arg" appends it, "stdin" pipes it
    exec_input: str = "arg"
    # how to pull text out of exec output: "text" | "json" | "stream-json"
    exec_output: str = "text"
    exec_json_path: str = ""      # dotted path into the JSON result, e.g. "result"

    persona: str = ""
    # "heavy" agents get the long / expensive work, "light" ones get quick takes.
    tier: str = "heavy"
    # Silence after the last visible update or in-flight tool, not a wall
    # clock from start. Progress slides the window; a busy 20-minute turn
    # is fine. Non-streaming exec still uses this as a process-lifetime cap.
    timeout: int = 900
    # Maximum silence between ACP updates. Non-positive disables the idle limit.
    acp_idle_timeout: float = 180.0
    env: dict[str, str] = field(default_factory=dict)

    # --- routing ---
    # Agents to try, in order, when this one refuses. Empty = use global rank.
    fallback: list[str] = field(default_factory=list)
    # Which quota probe knows how to read this CLI:
    # "codex" | "grok" | "claude" | "cursor" | "" (none).
    quota_probe: str = ""
    # Your own ceiling, used when the CLI reports nothing. Turns, not tokens -
    # crude, but it is the only unit we can count reliably from outside.
    budget_5h_turns: int | None = None
    budget_week_turns: int | None = None

    def matches(self, token: str) -> bool:
        token = token.lower().lstrip("@")
        return token == self.key or token in {a.lower() for a in self.aliases}

    @property
    def launch(self) -> list[str]:
        """Command used to sit the user in the agent's own TUI."""
        return list(self.interactive_command or (self.exec_command[:1] if self.exec_command else self.acp_command[:1]))

    @property
    def binary(self) -> str | None:
        """The CLI users actually install - not the npx/uvx shim in front of it."""
        cmd = self.launch or self.exec_command or self.acp_command
        return cmd[0] if cmd else None

    @property
    def installed(self) -> bool:
        return bool(self.binary) and shutil.which(self.binary) is not None


@dataclass
class Routing:
    """How a turn gets assigned, and what happens when that agent refuses."""

    # headroom | order | cheapest | sticky | lag_waste
    strategy: str = "headroom"
    # Tie-breaker and the fallback order when nothing reports quota.
    order: list[str] = field(
        default_factory=lambda: ["claude", "gpt", "cursor", "grok",
                                 "antigravity"])
    coding_keys: list[str] = field(
        default_factory=lambda: ["gpt", "grok", "cursor", "antigravity"])
    plan_key: str = "claude"
    cu_key: str = "gpt"
    heavy_key: str = "grok"
    max_attempts: int = 3            # agents tried before giving up on a turn
    # After a refused attempt, tell the next agent the workspace may be dirty.
    # Same idea as usher's continuation_guard; axis here is still lag+waste.
    continuation_guard: bool = True

    # circuit breaker
    trip_after: int = 2              # consecutive failures before tripping
    base_cooldown: float = 120.0     # first backoff, doubles each further trip
    max_cooldown: float = 3600.0
    auth_cooldown: float = 900.0     # a login problem will not fix itself fast
    quota_blind_cooldown: float = 1800.0   # "out of quota" with no reset time

    # scoring weights for strategy = "headroom"
    headroom_weight: float = 1.0
    priority_weight: float = 0.3
    cheap_bonus: float = 0.0         # raise to prefer light agents when free
    latency_weight: float = 0.2
    failure_penalty: float = 0.25
    # lag_waste: catch-up rate (lag / hours-to-reset) lets an overdue short
    # window rise quickly without making every fresh 5-hour window urgent.
    lag_weight: float = 0.5
    waste_weight: float = 1.0

    quota_ttl: float = 300.0         # seconds to reuse a quota snapshot
    # Hard caller-side budget for one agent's complete quota probe. Sync
    # probes run outside asyncio's default executor so this deadline also
    # bounds asyncio.run()/Ctrl-C shutdown latency.
    quota_probe_timeout: float = 20.0
    # Bound creation and buffered delivery of a UI/network event sink.
    event_sink_timeout: float = 30.0


@dataclass
class Sub2API:
    """Optional admin API used to read Codex 5h/7d that session logs omit."""

    base_url: str = ""
    admin_key: str = ""
    gpt_account: str = ""            # id or name; empty = openai oauth + Codex extra

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.admin_key.strip())


@dataclass
class Config:
    agents: list[AgentSpec]
    telegram_token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)
    default_workdir: str = str(Path.home())
    data_dir: str = str(DEFAULT_DATA_DIR)
    # IANA name for the /quota clock. Empty = this machine's timezone.
    timezone: str = ""
    source_path: str = ""  # toml that loaded; empty = defaults
    transcript_turns: int = 24          # how much history each agent sees
    auto_reply: bool = False            # respond in groups without an @mention
    stream_edit_interval: float = 1.5   # seconds between Telegram message edits
    max_parallel: int = 4
    debate_rounds: int = 1              # parallel argument rounds before judging
    debate_turn_timeout: float = 180.0  # debate should not inherit 15-30m job limits
    debate_judge_key: str = "cursor"
    routing: Routing = field(default_factory=Routing)
    sub2api: Sub2API = field(default_factory=Sub2API)

    def find(self, token: str) -> AgentSpec | None:
        for a in self.agents:
            if a.enabled and a.matches(token):
                return a
        return None

    def enabled_agents(self, tier: str | None = None) -> list[AgentSpec]:
        return [
            a for a in self.agents
            if a.enabled and (tier is None or a.tier == tier)
        ]

    def coding_agents(self) -> list[AgentSpec]:
        found: list[AgentSpec] = []
        for key in self.routing.coding_keys:
            spec = self.find(key)
            if spec is not None:
                found.append(spec)
        return found

    def plan_agent(self) -> AgentSpec | None:
        return self.find(self.routing.plan_key)

    def cu_agent(self) -> AgentSpec | None:
        return self.find(self.routing.cu_key)

    def heavy_agent(self) -> AgentSpec | None:
        return self.find(self.routing.heavy_key)


# --- built-in defaults -------------------------------------------------------
# Commands are best-effort defaults for the four subscription CLIs. `agora
# doctor` probes each one; override anything that does not match your install.

BUILTIN_AGENTS: list[dict[str, Any]] = [
    {
        "key": "claude",
        "label": "Claude",
        "emoji": "C",
        "aliases": ["cc", "claude-code", "sonnet", "opus"],
        "acp_command": ["npx", "-y", "@agentclientprotocol/claude-agent-acp"],
        "interactive_command": ["claude"],
        "exec_command": ["claude", "-p", "--output-format", "json",
                         "--dangerously-skip-permissions"],
        "persona": "You are Claude Code. Implement in the working directory "
                   "with your tools. Do not stop at a plan unless asked.",
        "exec_output": "json",
        "exec_json_path": "result",
        "tier": "heavy",
        "fallback": ["gpt", "cursor"],
        "quota_probe": "claude",
        "budget_5h_turns": 40,
        "budget_week_turns": 400,
    },
    {
        "key": "gpt",
        "label": "Codex",
        "emoji": "G",
        "aliases": ["codex", "openai", "chatgpt"],
        "acp_command": ["npx", "-y", "@agentclientprotocol/codex-acp@1.6.2"],
        "interactive_command": ["codex"],
        "exec_command": ["codex", "exec", "--json",
                         "--dangerously-bypass-approvals-and-sandbox"],
        "persona": "You are Codex. Implement in the working directory with "
                   "your tools. Do not stop at a plan unless asked.",
        "exec_output": "stream-json",
        "tier": "heavy",
        # Codex 5h/7d: Sub2API admin when configured, else ~/.codex/sessions.
        "quota_probe": "codex",
        "fallback": ["claude", "cursor"],
    },
    {
        "key": "grok",
        "label": "Grok",
        "emoji": "X",
        "aliases": ["xai", "grokbuild"],
        "acp_command": ["grok", "agent", "stdio"],
        "interactive_command": ["grok"],
        "prompt_as_arg": False,
        "exec_command": ["grok", "--output-format", "json",
                         "--always-approve", "--permission-mode",
                         "bypassPermissions", "-p"],
        "exec_output": "json",
        "exec_json_path": "result",
        "tier": "heavy",
        "timeout": 1800,
        "quota_probe": "grok",
        "fallback": ["cursor", "gpt"],
        "budget_5h_turns": 15,
        "budget_week_turns": 120,
        "persona": "You are Grok Build. Implement in the working directory "
                   "with your tools. Do not stop at a plan unless asked.",
    },
    {
        "key": "cursor",
        "label": "Cursor",
        "emoji": "K",
        "aliases": ["composer", "cursor-agent"],
        "acp_command": ["cursor-agent", "--model", "grok-4.6", "acp"],
        "interactive_command": ["cursor-agent", "--model", "grok-4.6"],
        "exec_command": ["cursor-agent", "-p", "--output-format", "json",
                         "--model", "grok-4.6", "--force"],
        "exec_output": "json",
        "exec_json_path": "result",
        "tier": "heavy",
        "fallback": ["gpt", "grok"],
        "quota_probe": "cursor",
        "budget_week_turns": 400,
        "persona": "You are Cursor Agent on Grok 4.6 (Ultra first-party). "
                   "Implement in the working directory. Do not switch to "
                   "Claude or GPT models.",
    },
    {
        "key": "antigravity",
        "label": "Antigravity",
        "emoji": "A",
        "aliases": ["agy", "antigrav", "google"],
        # agy 1.1.19 has no ACP mode, so this one is exec-only. That also
        # means it never holds a live session and never sticks.
        "transport": "exec",
        "interactive_command": ["agy"],
        # `agy` answers 0 even when it fails; the JSON carries `error`, which
        # the exec runner already classifies. `--print-timeout` must cover the
        # spec timeout or agy gives up at its own 5m default first.
        "exec_command": ["agy", "--output-format", "json",
                         "--dangerously-skip-permissions",
                         "--print-timeout", "15m",
                         "--model", "gemini-3.1-pro-high", "-p"],
        "exec_output": "json",
        "exec_json_path": "response",
        "tier": "heavy",
        "timeout": 900,
        "fallback": ["gpt", "cursor"],
        # No vendor usage endpoint is known. Ranking uses the local ledger
        # against these budgets; /quota draws them as estimated local.
        "budget_5h_turns": 30,
        "budget_week_turns": 300,
        "persona": "You are Antigravity CLI on first-party Gemini. Implement "
                   "in the working directory with your tools. Do not switch "
                   "to Claude or GPT models.",
    },
]


def _agent_from_dict(d: dict[str, Any]) -> AgentSpec:
    known = {f for f in AgentSpec.__dataclass_fields__}
    return AgentSpec(**{k: v for k, v in d.items() if k in known})


def load(path: str | os.PathLike[str] | None = None) -> Config:
    raw: dict[str, Any] = {}
    source_path = ""
    candidates = [Path(path)] if path else DEFAULT_CONFIG_PATHS
    for p in candidates:
        if p.is_file():
            if tomllib is None:
                raise SystemExit(
                    f"{p} exists but no TOML parser is available.\n"
                    "Use Python 3.11+, or `pip install tomli`.")
            raw = tomllib.loads(p.read_text())
            source_path = str(p)
            break

    overrides = {a["key"]: a for a in raw.get("agent", [])}
    agents: list[AgentSpec] = []
    for base in BUILTIN_AGENTS:
        merged = {**base, **overrides.pop(base["key"], {})}
        agents.append(_agent_from_dict(merged))
    for extra in overrides.values():          # fully custom agents
        agents.append(_agent_from_dict(extra))

    tg = raw.get("telegram", {})
    gen = raw.get("leftover", raw.get("agora", {}))
    s2 = raw.get("sub2api", {})
    known_routing = set(Routing.__dataclass_fields__)
    routing = Routing(**{k: v for k, v in raw.get("routing", {}).items()
                         if k in known_routing})
    routing.event_sink_timeout = max(1.0, float(routing.event_sink_timeout))
    gpt_account = os.environ.get("SUB2API_GPT_ACCOUNT", s2.get("gpt_account", ""))
    return Config(
        agents=agents,
        telegram_token=(os.environ.get("LEFTOVER_TELEGRAM_TOKEN")
                        or os.environ.get("AGORA_TELEGRAM_TOKEN")
                        or tg.get("token", "")),
        allowed_user_ids=[int(x) for x in tg.get("allowed_user_ids", [])],
        default_workdir=os.path.expanduser(gen.get("default_workdir", str(Path.home()))),
        data_dir=os.path.expanduser(
            gen.get("data_dir", str(DEFAULT_DATA_DIR))),
        timezone=str(gen.get("timezone", "") or "").strip(),
        transcript_turns=int(gen.get("transcript_turns", 24)),
        auto_reply=bool(gen.get("auto_reply", False)),
        stream_edit_interval=float(gen.get("stream_edit_interval", 1.5)),
        max_parallel=max(1, int(gen.get("max_parallel", 4))),
        debate_rounds=max(1, int(gen.get("debate_rounds", 1))),
        debate_turn_timeout=max(
            1.0, float(gen.get("debate_turn_timeout", 180.0))),
        debate_judge_key=str(gen.get("debate_judge_key", "cursor")).strip(),
        routing=routing,
        source_path=source_path,
        sub2api=Sub2API(
            base_url=os.environ.get("SUB2API_BASE_URL", s2.get("base_url", "") or "").strip(),
            admin_key=os.environ.get("SUB2API_ADMIN_API_KEY", s2.get("admin_key", "") or "").strip(),
            gpt_account="" if gpt_account is None else str(gpt_account).strip(),
        ),
    )
