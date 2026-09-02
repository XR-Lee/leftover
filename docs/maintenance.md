# Maintenance

Policy for the leftover `0.1.x` line. Product inventory is [Core features](core-features.md). What may grow is [Roadmap](roadmap.md). Shape is [Architecture](architecture.md). Why is [history/decisions.md](history/decisions.md).

## Git root

This directory is the leftover git root (`github.com/XR-Lee/leftover`). Do not init the mixed parent folder (`Projects/MacBot` with AutoPaperReview / paper_review).

`.venv/` stays untracked. Decision log lives in `docs/history/`.

## Compatibility promises

### CLI

- `leftover` is the product entry. `agora` remains for doctor / frozen Telegram.
- Additive flags may ship in 0.1.x. Removing `--pick` / `--print` / `--use` / `--agent` / `--tui` / `--why` is a breaking change. `-p` is an alias of `--print`. `leftover scope` is additive (D19). `--heavy` / `kind: heavy` are additive (D20). Heavy execution is parallel independent + compare-notes (D21).
- `--agent` must never become an alias of `--use`.
- `leftover --pick --json` field names `kind`, `agent`, `agents`, `announce`, `run`, `completion`, `spawn`, `chain`, `reason`, `scope` are a skill ABI. Rename only with a skill bump and a test. `scope.active=false` must bypass routing before probes, keep the caller on itself, and return null `run` / `spawn` so a cached skill cannot leftover --print itself. An empty `--agent` is that same bypass. A self-pick (`agent` equals `self`) also returns null `run`.
- Bare `--json` (no `-p`) stays a pick dump. `leftover -p --json` is a run envelope (`agent`, `kind`, `exit_code`, `output`, `attempts`).
- `leftover quota --json` is the `/quota` windows (`source`, `used_percent`, `resets_at`, `note`). `leftover --why --json` is the lag+waste table (`lag`, `waste`, `total`). Neither is a pick dump. Neither grows a strength field.

### Agents

- Built-in keys `claude` `gpt` `grok` `cursor` stay stable. Aliases may grow.
- Cursor ACP/exec must keep `--model grok-4.6` unless Ultra first-party Grok is replaced on purpose.
- Computer use stays Codex (`routing.cu_key`). Adding a second CU backend needs a new decision, not a silent fallback.
- Coding pool stays `routing.coding_keys`. Claude stays out of lag_waste.
- Disk-persisted cwd history must not override a fresh quota ranking. Sticky is
  reserved for a successful coding route whose live session is owned by the
  current process; debate/warmup sessions do not establish that provenance.

### Quota

- Window `source` is `reported` | `observed` | `estimated`. `/quota` must label it.
- `/quota` renders in English, and its clock is this machine's timezone unless
  `[leftover] timezone` names one. Do not hardcode a zone in `rhythm.render`.
- Estimated windows must not contribute `waste`.
- `waste` is lag divided by hours-to-reset. Do not score the whole unused pool
  as urgent at the instant a short window resets.
- `score_quota.total` is the allocation window (weekly, else monthly, else a
  session-only bucket). A live 5h/session window must not set `total` and must
  not ahead-gate the agent. A 5h at 100% is `session_blocked` (skip), then a
  tie-break only when allocation totals match to 0.001.
- Probe failures degrade; they do not crash pick.
- Every agent probe shares one `routing.quota_probe_timeout` deadline (20 seconds by default). Internal pagination and fallback probes must consume the remaining budget, not start fresh deadlines.
- Synchronous probes must use the bounded daemon probe pool, not asyncio's default executor; timeout and Ctrl-C must not delay event-loop shutdown. A timed-out worker may finish its own bounded I/O in the background.
- A silent or timed-out refresh must retain unexpired `reported` / `observed` data instead of replacing it with an empty estimate.
- Sub2API is admin GET only. Never send completions through it. Keys live in `~/.config/leftover/leftover.toml` (0600) or `SUB2API_ADMIN_API_KEY`, never in the repo.

### Transport

- `transport = "auto"`: ACP if the command exists, else exec; ACP start failure closes the process and retries exec.
- `agy` has no ACP mode, and two concurrent `agy -p` runs share one
  background language server: one adopts the other's workspace, or one dies
  with `context canceled`. leftover serializes turns per agent, so it is
  safe on its own — but a second leftover, or the Antigravity IDE running
  beside it, will cancel turns. Do not add parallel antigravity slots.
- ACP start, cancel, close, full-turn timeout, and idle timeout must all have finite caller-visible bounds. Cleanup order is process first, SDK transport second.
- ACP and exec subprocesses use isolated POSIX process groups. Timeout, cancellation, close, and failed startup terminate the whole tree with TERM then KILL while draining inherited stdout/stderr pipes; killing only the leader is not sufficient.
- ACP filesystem callbacks use a bounded daemon pool so blocked OS I/O cannot hold the event loop or interpreter open. A queued write that has not started is skipped after timeout. A write already inside synchronous OS I/O cannot be revoked; its timeout must say the outcome is uncertain and the write may complete later, so callers inspect the file before retrying.
- Every ACP connection owns its event queue. A cancelled or superseded lifecycle generation must not publish a session or deliver late updates into a later turn.
- A live ACP runner and session are reused for later explicit turns on the same agent and workdir. A prompt-level connection failure retires that runner so the next turn creates a clean session.
- Whole-turn and ACP idle timeouts are terminal for the request. Do not replay a possibly state-changing task on a different backend; quick startup/auth/quota/transient failures may still follow `max_attempts`. The turn deadline slides on visible progress and in-flight tools; it is not a start-of-turn wall clock. ACP idle is paused while a tool is in-flight; it resumes when that tool completes. Internal protocol activity still does not reset idle or the turn window.
- Exec `stream-json` timeout includes the period after stdout EOF while the child is still alive. Legal NDJSON records may exceed the default 64 KiB StreamReader limit. Per-turn metadata must be reset before every process.
- An exec leader's returncode is the command boundary. If descendants retain inherited stdout/stderr after that exit, terminate the saved process group, drain the captured output, and complete the turn instead of waiting for the full model timeout.
- Piped stdin is sampled with a finite non-blocking read. Readability does not imply EOF, so `--print` must never call an unbounded read-to-EOF before routing.
- Event-sink creation, streamed callbacks, and buffered group delivery all have finite deadlines. A group shares one cumulative delivery budget that counts callback await time, not time spent waiting for slower models. Delivery failure is advisory and must not trip backend health.
- Terminal StreamSink instances share one bounded FIFO daemon writer. Synchronous TextIO cannot block the event loop or grow one thread per turn; cancelled jobs that have not started must not be written later.
- Per-agent turns are serialized. Shutdown closes admission before draining active work, rejects queued work, and must not deadlock with cancellation or a pending writer. Pool transition deadlines cover the whole operation, and lock/condition waits must preserve external cancellation on Python 3.10.
- Parallel fallback slots share one quota-aware ranking and claim distinct spares. A quota probe that started before a refusal must merge that newly observed limit; a later authoritative reported probe may replace an older undated refusal.
- Auto-approve stays the unattended default. Document it; do not add an interactive permission prompt in v0.1.

### Credentials

- Probes reuse the vendor's own login and reach only that vendor's host. Any new
  probe belongs in `SECURITY.md`'s read/send/write tables in the same commit.
- A secret never travels as an argv element (`ps` is world-visible per user).
  Keychain writes feed `security add-generic-password -w` on stdin.

### Persistence

- `leftover-state.json` and `ledger.json` are shared across processes. Writers must take `flock`, re-read under the lock, merge per cwd/agent entry, and publish with `os.replace`.
- State and quota persistence are advisory. Empty, damaged, structurally invalid, locked, or unwritable files must degrade without blocking a routed task.

## What a change is allowed to do

Allowed without a new decision:

- Fix a vendor field rename in `quota.py` plus a parser test.
- Pin a drifted ACP argv in `BUILTIN_AGENTS` plus `test_builtin_acp_commands`.
- Add a test for an existing invariant.
- Quiet UI / rhythm copy that does not change pick.

Needs a new entry in `docs/history/decisions.md`:

- New default mode (for example making `--tui` the REPL default).
- A Grok/Codex/Cursor plugin beyond `install-skills`.
- Replacing runners with acpbot/OpenACP (D6), or folding the product into usher (D13).
- A sixth built-in agent (Antigravity is already the fifth; D17).
- Growing Telegram, Discord, or any second frontend.
- Forking a vendor TUI.

Growth that is not a bugfix must pass the [roadmap](roadmap.md) filter
(D18): official remaining, lag+waste, or the parent conversation.

Never:

- Reverse-proxy a subscription as OpenAI-compatible completions.
- Fork `xai-org/grok-build` or `openai/codex` to swap backends.
- Route Cursor onto Claude or GPT models.
- Print a classified limit message as the answer.
- Call a benched agent “to see if it recovered” except the half-open probe the breaker already owns.

## Tests

No network, no subscription spend:

```bash
cd leftover
python tests/test_macbot.py
python tests/test_routing.py
python tests/test_e2e.py
python tests/test_telegram.py
python tests/test_reliability.py
python tests/test_state_reliability.py
python -m compileall -q leftover tests
```

`test_macbot.py` is the product contract (intent, score, pick JSON, `--print` chain, ACP lifecycle, quiet in-flight tool must not exit 124, REPL tab completes commands and `@name`). `test_routing.py` is probes, classification, timeout/fallback policy, and ACP process cleanup. `test_e2e.py` is group modes on mock ACP. `test_telegram.py` covers thought isolation, bounded status metadata, and complete HTML chunks. `test_reliability.py` covers quota deadlines, ledger concurrency, stream-json EOF hangs, and per-turn exec metadata. `test_state_reliability.py` covers multi-process state merging and atomic reads.

A quota-parser change that only updates `quota.py` is incomplete. Add a fixture-shaped payload next to the existing `parse_*` tests.

ACP runner changes must keep:

- failed handshake closes the transport
- concurrent `start` is a singleton
- cancel then prompt ignores stale `done`
- process-first close lets an owning `asyncio.run()` exit
- TERM/KILL reaches descendants that inherit the adapter's stdio
- filesystem callbacks stay bounded, and in-flight write timeouts disclose possible late completion
- start/cancel/close have caller-visible hard bounds
- every connection has an isolated update queue
- queued runs cannot begin after shutdown admission closes
- concurrent run, `/cd`, cancellation, and shutdown do not deadlock
- workdirs do not cross
- exec structured errors are failures

Router/group changes must keep:

- successful explanations of 401/429/500/timeout do not trigger fallback
- whole-turn and idle timeout do not cross backends
- a quiet in-flight ACP tool must not make `--print` exit 124
- debate role timeout does not replay on a spare
- broadcast work stays parallel while emitted answers remain grouped
- group delivery budgets exclude model compute gaps
- sink creation and delivery timeout without penalizing backend health
- concurrent quota probes preserve newer observed refusals

## Dependencies

Keep the install small:

- `agent-client-protocol` — ACP client
- `tomli` on Python < 3.11
- `python-telegram-bot` — optional `[telegram]` extra only, for the frozen
  transport; do not add Discord/Slack SDKs on top, and do not move it back into
  the required set

Python 3.10 is the floor (macOS system 3.9 is not enough). Do not raise the floor in a patch release.

Vendor CLIs are not Python dependencies. `doctor` tells the operator what is missing.

## Security

- Telegram `allowed_user_ids` is mandatory if `agora bot` is used. Empty allow-list is “anyone who finds the bot runs agents as you”.
- Working directory is trusted. Agents edit files with permissions bypassed. `/cd` only onto a git checkout you accept losing.
- Quota probes may read Keychain and `state.vscdb`. Do not log tokens, cookies, or full OAuth blobs.
- Do not commit `macbot.toml` with `admin_key` or Telegram tokens.

## Release checklist (when a repo exists)

1. Run the six test files above plus `compileall`.
2. `leftover doctor` on a machine with at least one CLI.
3. Diff `BUILTIN_AGENTS` ACP/exec argv against `test_builtin_acp_commands`.
4. Confirm `install-skills` still symlinks, not copies. `leftover scope off`
   must unlink the canonical path plus only recognizably leftover-owned legacy
   `skills/macbot` entries. A foreign `macbot` skill must survive.
5. Note any vendor field rename in `notes/platform-notes.md`.
6. Do not tag a release from the mixed parent folder.
