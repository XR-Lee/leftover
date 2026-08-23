# Architecture

Implemented shape of the `0.1.x` tree. Planned work stays in [Maintenance](maintenance.md) and [history/decisions.md](history/decisions.md).

## Goals

- Spend subscription windows by driving the official CLIs, not by wrapping OAuth as completions.
- Keep one parent conversation. Subagents are ACP (or exec) processes with their own tools and sessions.
- Pick with lag+waste when the vendor publishes remaining quota; always fall back when a turn is a refusal.
- Stay small enough that a future git repo is this directory, not a fleet product.
- Do not rewrite the launcher. [usher](https://github.com/theodorebeaupre-prog/usher) already execs the vendor TUI. `--tui` / `--why` / `continuation_guard` align with it; remaining-quota and lag+waste do not.

## Non-goals

- A Grok/Codex/Cursor pager, plugin host, or fork (D8).
- A reverse-proxy API, OpenRouter, or “unified model id” (D1).
- A hosted bot fleet, pane multiplexer, or second chat frontend as the default (D7).
- Growing Telegram until D6 is resolved. The transport works; it is not the product.

## System boundary

```text
human / Warp / Cursor / Claude / Grok skill
        |
        v
  leftover  (classify + pick + REPL or --print or --tui)
        |
        +-- intent.kind
        |     coding | plan | computer_use
        |     roundtable | broadcast | debate | relay
        |
        +-- Router.rank  (lag_waste on coding pool)
        |     + quota probes (vendor login, read-only)
        |     + Health / circuit breaker
        |
        +-- AgentPool
              ACP runner  -->  claude-agent-acp / codex-acp /
                               grok agent stdio / cursor-agent acp
              exec fallback --> claude -p / codex exec / grok -p /
                                cursor-agent -p
```

The package still ships `agora bot` (Telegram) on the same orchestrator, behind the optional `[telegram]` extra. That path is leftover. Do not add Telegram-only features.

## Runtime pieces

| Piece | File | Responsibility |
|---|---|---|
| CLI | `macbot.py` (CLI module) | argv, REPL, `--pick` JSON, `-p`/`--print`, `--tui`, `--why`, `--timeout`, skills |
| Intent | `intent.py` | slash / `@` / CU phrases → `Intent` |
| Score | `score.py` | lag, waste, one number per agent |
| Quota | `quota.py` | probes, `classify()`, ledger, Window/Quota serde |
| Router | `router.py` | rank, fallback inside one request, health |
| Rhythm | `rhythm.py` | `/quota` calendar-vs-usage text |
| Orchestrator | `orchestrator.py` | group modes + shared transcript |
| Transcript | `transcript.py` | last N messages, 1200-char trim |
| Pool | `agents/` | one live runner per agent; ACP→exec on start failure |
| Config | `config.py` | `~/.config/leftover/leftover.toml` then agora.toml; builtins |
| UI | `ui.py` | seat/failover chrome, `StreamSink` |
| Skill | `skills/leftover/SKILL.md` | how a vendor CLI re-enters leftover |
| Doctor | `doctor.py` | roster, cached remaining, install hints, paths |

State on disk:

- `~/.local/share/leftover/leftover-state.json` — route history, health, last quota snapshot
- `~/.local/share/leftover/ledger.json` — turn counts for estimated budgets
- `~/.local/share/leftover/history` — REPL readline

Old `~/.local/share/agora/macbot-state.json` is still read for one version.

Config search order: `~/.config/leftover/leftover.toml`, `~/.config/macbot/macbot.toml`, `~/.config/agora/agora.toml`, `./leftover.toml`, `./macbot.toml`, `./agora.toml`.

## Pick JSON (skill contract)

`leftover --pick --json --agent <self> <task>` returns at least:

| Field | Meaning |
|---|---|
| `kind` | `coding` `plan` `computer_use` `roundtable` `broadcast` `debate` `relay` |
| `agent` | chosen key, or null for a group panel |
| `agents` | group panel keys |
| `announce` | the one line the human should see (`leftover · Cursor`) |
| `chain` | fallback order for this request |
| `reason` | why this pick (debug; do not print to the human) |
| `run` | foreground argv the skill should execute, or null when unavailable; never synthesize it or use the TUI `spawn` argv |
| `completion` | process-exit wait contract for the parent execution handle |
| `spawn` | vendor TUI argv; skills must not run this |
| `self` | echo of `--agent` |

`--agent` is who is asking. `--use` is who should go first.

### Parent handoff wait contract

Both the initial `--pick --json` query and the argv in `run` are synchronous and
foreground. If either yields an execution handle, the parent waits on that same
handle instead of starting another pick or handoff. The `leftover --print`
process writes progress to stderr, writes the final answer to stdout, then
exits. Process exit is the only completion callback across this boundary; a
stderr heartbeat cannot wake a parent agent that is not waiting on the command
handle.

When an execution tool yields a still-running `session_id`, `cell_id`, or job
handle, the parent must immediately use the host's blocking wait on that same
handle; a correct wait returns as soon as the process exits. If the host only
offers non-blocking polling, the `completion.max_poll_interval_seconds` value
bounds the gap at 10 seconds. The parent must not schedule the next poll from a
model-generated runtime estimate, sleep for minutes, detach the command, or
launch a duplicate.

## Request path

1. `intent.parse` — tags beat scoring; `@any` is unnamed.
2. `decide` forces `strategy = lag_waste`. Only an unnamed coding follow-up
   with process-local success provenance and a live session is sticky.
3. Group modes build a panel (`discussion_panel` / `debate_panel`) and stop. They do not pick one agent.
4. `Router.run` walks `ordered_chain` (the pick), skipping benched keys. It does not re-inject per-agent `fallback` when the chain is pinned.
5. `observe` classifies error **and** short result text. Quota refusal benches until `resets_at` or `quota_blind_cooldown`.
6. REPL follow-up on a live ACP session sends the bare user line. First turn on a new session gets WORK/PLAN_ONLY plus trimmed leftover history.

## Built-in agents

Defined in `config.BUILTIN_AGENTS`. Keys are stable: `claude`, `gpt`, `grok`, `cursor`.

| Key | Interactive | ACP (current pin) | Coding pool |
|---|---|---|---|
| `gpt` | `codex` | `npx -y @agentclientprotocol/codex-acp@1.6.2` | yes |
| `grok` | `grok` (no argv prompt) | `grok agent stdio` | yes |
| `cursor` | `cursor-agent --model grok-4.6` | same + `acp` | yes |
| `claude` | `claude` | `npx -y @agentclientprotocol/claude-agent-acp` | no (plan + last resort) |

Vendor flags drift. Change the pin in builtins **and** `test_builtin_acp_commands`. Do not add a fifth agent unless it speaks ACP or a headless print mode and has a quota probe or an honest estimated budget.

## Two routers, one job split

Routing (who goes first) and fallback (who takes a refusal) are separate (D5).

leftover coding always uses `lag_waste`. Telegram `agora bot` still honors `[routing].strategy` in toml (`headroom` / `order` / `cheapest` / `sticky`). Do not collapse those names into one function just to tidy the file.
