# Architecture

Implemented shape of the `0.1.x` tree. Planned work stays in [Roadmap](roadmap.md). Policy is [Maintenance](maintenance.md). Why is [history/decisions.md](history/decisions.md).

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
        |     roundtable | broadcast | debate | relay | heavy
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
| Skill scope | `scope.py` | canonical per-caller switch, legacy `skills/macbot` migration, `leftover scope` / `/scope` |
| Intent | `intent.py` | slash / `@` / CU phrases → `Intent` |
| Score | `score.py` | lag, waste, one number per agent |
| Quota | `quota.py` | probes, `classify()`, ledger, Window/Quota serde |
| Router | `router.py` | rank, fallback inside one request, health |
| Rhythm | `rhythm.py` | `/quota` calendar-vs-usage text |
| Orchestrator | `orchestrator.py` | group modes + shared transcript + optional progress observer |
| Transcript | `transcript.py` | last N messages, 1200-char trim |
| Pool | `agents/` | one live runner per agent; ACP→exec on start failure |
| Config | `config.py` | `~/.config/leftover/leftover.toml` then agora.toml; builtins |
| UI | `ui.py` | seat/failover chrome, phase-aware group `Roster`, `StreamSink` |
| Skill | `skills/leftover/SKILL.md` | how a vendor CLI re-enters leftover and honors the live scope result |
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
| `kind` | `coding` `plan` `computer_use` `heavy` `roundtable` `broadcast` `debate` `relay` |
| `agent` | chosen key, or null for a group panel |
| `agents` | group panel keys |
| `announce` | the one line the human should see (`leftover · Cursor`) |
| `chain` | fallback order for this request |
| `reason` | why this pick (debug; do not print to the human) |
| `run` | foreground argv the skill should execute, or null when unavailable; never synthesize it or use the TUI `spawn` argv |
| `completion` | process-exit wait contract for the parent execution handle |
| `spawn` | vendor TUI argv; skills must not run this |
| `self` | echo of `--agent` |
| `scope` | canonical caller switch (`active`, `path`, and any owned legacy paths) |

`--agent` is who is asking. `--use` is who should go first.
When `scope.active` is false, the current vendor handles the task directly and
must not announce or execute a leftover handoff. `run`, `spawn`, and
`announce` are empty. The gate runs before quota probing and is checked again
before the pick is printed, because vendor skill directories overlap and live
sessions can retain an older skill listing. An explicit empty `--agent`
bypasses the same way. A pick that omits `--agent` still routes unless
leftover is off for every CLI. When leftover is on and `agent` equals
`self`, `run` is also empty so the caller cannot leftover --print itself.

### Parent handoff wait contract

Both the initial `--pick --json` query and the argv in `run` are synchronous and
foreground. If either yields an execution handle, the parent waits on that same
handle instead of starting another pick or handoff. The `leftover --print`
process writes progress to stderr (tools, in-progress plan, compact thought,
heartbeat), writes the final answer to stdout, then exits. Process exit is the only completion callback across this boundary; a
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
3. Group modes build a panel (`discussion_panel` / `debate_panel` / `heavy_panel`) and stop. They do not pick one agent. Heavy is two parallel rounds (independent, then compare-notes), not a sequential roundtable.
4. `Router.run` walks `ordered_chain` (the pick), skipping benched keys. It does not re-inject per-agent `fallback` when the chain is pinned.
5. `observe` classifies error **and** short result text. Quota refusal benches until `resets_at` or `quota_blind_cooldown`.
6. REPL follow-up on a live ACP session sends the bare user line. First turn on a new session gets WORK/PLAN_ONLY plus trimmed leftover history.

### Group progress observer

`orchestrator.GroupProgress` is a transport-neutral optional observer.
`Orchestrator.execute(..., progress=None)` has no terminal UI side effects;
CLI `_discuss` and `agora console` opt in by creating a `ui.Roster` and passing
it to the orchestrator.
Built-in buffered answer sinks separately opt in with
`leftover_turn_status=True` to a compact `status` caption for role, round,
elapsed time and tool count. Unmarked sinks keep the prior `tool` / `text` /
`error` / `done` event protocol.

| Mode | Observed phases and answer order |
|---|---|
| `/rt` | one sequential `shared context` phase; roles are `speaker i/n` |
| `/all` | one parallel `independent answers` phase; `member` answers are emitted as grouped blocks in completion order |
| `/debate` | parallel `arguments` phases (`for` / `against`), then a sequential `verdict` (`judge`) |
| `/heavy` | parallel `independent` (`leader` / `worker`), then parallel `compare-notes` (`synthesis` / `discuss`); answers emit in seat order after each phase |
| `/relay` | three sequential phases: `plan`, `implement`, `review` |

The roster header reports mode, phase `x/y`, phase name, finished/failed counts,
parallel/sequential shape and elapsed time. Each stable seat reports CLI
badge/name, role, lifecycle state, compact activity, elapsed time and tool
count. A fallback leaves the original row as `replaced` with `continued by
<CLI>` and continues under the same seat identity. Terminal display states are
ready/done, failed, timeout, stopped and empty. Runtime terminal and redirected
output uses append-only phase/row logs plus a heartbeat, so a blocked writer
cannot later erase an answer with cursor control. Width-aware preview rows clip
by terminal cells and hide roles when space is tight; destructive snapshots are
limited to the synchronous in-memory renderer used by deterministic tests.

Pool lifecycle events (`queued`, `preparing`, `running`, with a structured
`turn_id`) are emitted only when an event callback opts in with
`leftover_lifecycle=True`. Normal sinks and transports therefore do not receive
the extra lifecycle stream.

## Turn lifecycle and completion

`AgentPool.submit()` creates a `TurnHandle` before its worker can run. The
handle moves through `queued`, `running`, and exactly one terminal state:
`completed`, `error`, `timed_out`, or `cancelled`. Its task deadline begins
when runner execution starts, not while the request is waiting for that
agent's serialized slot.

`handle.wait(timeout)` only bounds the observer. It never cancels the agent;
`handle.cancel()` is the explicit cancellation boundary. Terminal settlement
and resource cleanup are separate futures, so the parent callback can run as
soon as the outcome is known while the original worker finishes cancellation,
process reaping, and lock release. `wait_cleanup()` marks the end of the
worker's bounded cleanup phase and release of its serialized slot; a low-level
SDK or OS task may still be detached after its own hard cleanup deadline.
Cancelling the compatibility `pool.run()` entry point follows the same rule
and does not wait behind stubborn cleanup.

Published handles enter process-local FIFO completion inboxes isolated by
`parent_id`. Each owner queue is bounded at 256 entries and drops its oldest
completion on overflow; `completion_overflows()` exposes that loss. Empty
owner queues retire after their last waiter, and `discard_completions()` lets a
finished owner release unconsumed callbacks. These inboxes are callback
channels, not durable job storage. `pool.run()` keeps its existing `Turn`
return type and does not publish an inbox entry.

ACP prompts add a second, protocol-level boundary. Each prompt receives an
epoch, a terminal future, and an update gate before the prompt RPC starts.
Success, failure, timeout, and cancellation settle that future once; later
updates captured for the closed epoch are rejected. ACP notifications do not
carry a prompt id, so an uncertain cancellation still retires the whole
connection generation before another prompt can use it. One prompt epoch sends
at most one cancel RPC even when graceful pool cancellation is followed by a
hard worker cancellation.

Each ACP turn also owns one ordered event pump. The pump snapshots accepted
text, tools, and terminal error before publishing the `Turn`, so queued output
survives an immediate RPC failure and later callback cleanup cannot mutate an
already observed result. Error and cancellation settlement bypass a blocked
event sink; delivery remains cleanup-owned and preserves event order.

Pool startup cleanup is registered before `runner.start()` can yield. If a
startup is retired while still running, its finalizer issues one bounded early
close, waits for startup to become terminal, and closes again to catch resources
opened after the first race. A transition deadline bounds only its caller: the
same finalizer continues to own a close that is still cleaning up, repeated
shutdown does not launch a concurrent duplicate, and a completed close failure
is retried only by a later explicit transition. Close cancellation is sent
before the pool deadline so a coroutine that consumes one cancellation cannot
hold `asyncio.run()` teardown open.

Timeouts remain distinct: queue wait, task deadline, ACP visible-progress idle
deadline, event-sink delivery, and cleanup grace each have their own boundary.
The turn deadline is silence since the last visible update or in-flight tool,
not a wall clock from start, so a busy long job is not cut at 900s. Protocol
activity without visible thought, plan/status, tool, or text output
does not extend the ACP idle deadline or the turn deadline. Idle pauses while
a tool is in-flight
and restarts when the last one completes, so a quiet execute is not a hang.
Turn, idle, and event-sink timeouts
become visible to the parent before ACP abort cleanup; shutdown also owns
direct prepare/startup tasks so a warmup cannot retain the pool's read gate
unnoticed.

## Built-in agents

Defined in `config.BUILTIN_AGENTS`. Keys are stable: `claude`, `gpt`, `grok`, `cursor`, `antigravity`.

| Key | Interactive | ACP (current pin) | Coding pool |
|---|---|---|---|
| `gpt` | `codex` | `npx -y @agentclientprotocol/codex-acp@1.6.2` | yes |
| `grok` | `grok` (no argv prompt) | `grok agent stdio` | yes |
| `cursor` | `cursor-agent --model grok-4.6` | same + `acp` | yes |
| `antigravity` | `agy --model gemini-3.1-pro-high` | none (exec only) | yes, last |
| `claude` | `claude` | `npx -y @agentclientprotocol/claude-agent-acp` | no (plan + last resort) |

Vendor flags drift. Change the pin in builtins **and** `test_builtin_acp_commands`. Do not add a sixth agent unless it speaks ACP or a headless print mode and has a quota probe or an honest estimated budget.

## Two routers, one job split

Routing (who goes first) and fallback (who takes a refusal) are separate (D5).

leftover coding always uses `lag_waste`. Telegram `agora bot` still honors `[routing].strategy` in toml (`headroom` / `order` / `cheapest` / `sticky`). Do not collapse those names into one function just to tidy the file.
