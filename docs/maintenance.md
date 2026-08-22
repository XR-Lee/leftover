# Maintenance

Policy for the leftover `0.1.x` line. Product inventory is [Core features](core-features.md). Shape is [Architecture](architecture.md). Why is [history/decisions.md](history/decisions.md).

## Git root

This directory is the leftover git root (`github.com/XR-Lee/leftover`). Do not init the mixed parent folder (`Projects/MacBot` with AutoPaperReview / paper_review).

`.venv/` stays untracked. Decision log lives in `docs/history/`.

## Compatibility promises

### CLI

- `leftover` is the product entry. `agora` remains for doctor / frozen Telegram.
- Additive flags may ship in 0.1.x. Removing `--pick` / `--print` / `--use` / `--agent` / `--tui` / `--why` is a breaking change. `-p` is an alias of `--print`.
- `--agent` must never become an alias of `--use`.
- `leftover --pick --json` field names `kind`, `agent`, `agents`, `announce`, `run`, `spawn`, `chain`, `reason` are a skill ABI. Rename only with a skill bump and a test.
- Bare `--json` (no `-p`) stays a pick dump. `leftover -p --json` is a run envelope (`agent`, `kind`, `exit_code`, `output`, `attempts`).

### Agents

- Built-in keys `claude` `gpt` `grok` `cursor` stay stable. Aliases may grow.
- Cursor ACP/exec must keep `--model grok-4.6` unless Ultra first-party Grok is replaced on purpose.
- Computer use stays Codex (`routing.cu_key`). Adding a second CU backend needs a new decision, not a silent fallback.
- Coding pool stays `routing.coding_keys`. Claude stays out of lag_waste.

### Quota

- Window `source` is `reported` | `observed` | `estimated`. `/quota` must label it.
- Estimated windows must not contribute `waste`.
- Probe failures degrade; they do not crash pick.
- Every agent probe shares one `routing.quota_probe_timeout` deadline (20 seconds by default). Internal pagination and fallback probes must consume the remaining budget, not start fresh deadlines.
- Synchronous probes must use the bounded daemon probe pool, not asyncio's default executor; timeout and Ctrl-C must not delay event-loop shutdown. A timed-out worker may finish its own bounded I/O in the background.
- A silent or timed-out refresh must retain unexpired `reported` / `observed` data instead of replacing it with an empty estimate.
- Sub2API is admin GET only. Never send completions through it. Keys live in `~/.config/leftover/leftover.toml` (0600) or `SUB2API_ADMIN_API_KEY`, never in the repo.

### Transport

- `transport = "auto"`: ACP if the command exists, else exec; ACP start failure closes the process and retries exec.
- ACP start, cancel, close, full-turn timeout, and idle timeout must all have finite caller-visible bounds. Cleanup order is process first, SDK transport second.
- Every ACP connection owns its event queue. A cancelled or superseded lifecycle generation must not publish a session or deliver late updates into a later turn.
- Whole-turn and ACP idle timeouts are terminal for the request. Do not replay a possibly state-changing task on a different backend; quick startup/auth/quota/transient failures may still follow `max_attempts`.
- Exec `stream-json` timeout includes the period after stdout EOF while the child is still alive. Per-turn metadata must be reset before every process.
- Auto-approve stays the unattended default. Document it; do not add an interactive permission prompt in v0.1.

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
- A fifth built-in agent.
- Growing Telegram, Discord, or any second frontend.
- Forking a vendor TUI.

Never:

- Reverse-proxy a subscription as OpenAI-compatible completions.
- Fork `xai-org/grok-build` or `openai/codex` to swap backends.
- Route Cursor onto Claude or GPT models.
- Print a classified limit message as the answer.
- Call a benched agent “to see if it recovered” except the half-open probe the breaker already owns.

## Tests

No network, no subscription spend:

```bash
cd agora
python tests/test_macbot.py
python tests/test_routing.py
python tests/test_e2e.py
python tests/test_reliability.py
python tests/test_state_reliability.py
python -m compileall -q agora tests
```

`test_macbot.py` is the product contract (intent, score, pick JSON, `--print` chain, ACP lifecycle). `test_routing.py` is probes, classification, timeout/fallback policy, and ACP process cleanup. `test_e2e.py` is group modes on mock ACP. `test_reliability.py` covers quota deadlines, ledger concurrency, stream-json EOF hangs, and per-turn exec metadata. `test_state_reliability.py` covers multi-process state merging and atomic reads.

A quota-parser change that only updates `quota.py` is incomplete. Add a fixture-shaped payload next to the existing `parse_*` tests.

ACP runner changes must keep:

- failed handshake closes the transport
- concurrent `start` is a singleton
- cancel then prompt ignores stale `done`
- process-first close lets an owning `asyncio.run()` exit
- start/cancel/close have caller-visible hard bounds
- every connection has an isolated update queue
- workdirs do not cross
- exec structured errors are failures

Router/group changes must keep:

- successful explanations of 401/429/500/timeout do not trigger fallback
- whole-turn and idle timeout do not cross backends
- debate role timeout does not replay on a spare
- broadcast work stays parallel while emitted answers remain grouped

## Dependencies

Keep the install small:

- `agent-client-protocol` — ACP client
- `python-telegram-bot` — frozen Telegram; do not add Discord/Slack SDKs on top
- `tomli` on Python < 3.11

Python 3.10 is the floor (macOS system 3.9 is not enough). Do not raise the floor in a patch release.

Vendor CLIs are not Python dependencies. `doctor` tells the operator what is missing.

## Security

- Telegram `allowed_user_ids` is mandatory if `agora bot` is used. Empty allow-list is “anyone who finds the bot runs agents as you”.
- Working directory is trusted. Agents edit files with permissions bypassed. `/cd` only onto a git checkout you accept losing.
- Quota probes may read Keychain and `state.vscdb`. Do not log tokens, cookies, or full OAuth blobs.
- Do not commit `macbot.toml` with `admin_key` or Telegram tokens.

## Release checklist (when a repo exists)

1. Run the three test files above.
2. `leftover doctor` on a machine with at least one CLI.
3. Diff `BUILTIN_AGENTS` ACP/exec argv against `test_builtin_acp_commands`.
4. Confirm `install-skills` still symlinks, not copies.
5. Note any vendor field rename in `notes/platform-notes.md`.
6. Do not tag a release from the mixed parent folder.
