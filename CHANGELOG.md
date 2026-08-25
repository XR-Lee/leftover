# Changelog

Notable changes per release. Dates are release dates.

## Unreleased

### Tests

- **`--print` long-tool path is covered.** The 0.1.1 idle-pause fix had a
  runner-only example. `test_print_long_running_tool_does_not_exit_124` now
  drives a real mock ACP process through `run_print`: a quiet in-flight
  `pytest` lasts past `acp_idle_timeout`, the parent-style poll keeps seeing
  a live task, stderr heartbeats, and the exit code stays 0 instead of 124.

## 0.1.1 — 2026-08-25

Skill scope is a switch, not a one-way install. Long ACP tools no longer look
like hangs.

### Fixed

- **Long ACP tools no longer trip idle timeout.** leftover treated 180s
  without a new text/thought/tool event as a hang, so a quiet `pytest` /
  `cargo test` / similar in-flight tool killed the turn and leftover
  `--print` exited 124. Idle hang detection now pauses while a tool is
  in-flight and restarts when it completes. Internal protocol chatter
  still does not count as progress. `tests/test_routing.py` has the
  long-task example (`test_acp_long_running_tool_survives_idle_silence`).

### Added

- **`leftover scope`.** leftover's influence on other CLIs is the `SKILL.md`
  symlink in each vendor skill directory. That is now a switch, not a one-way
  install. `leftover scope` is a five-row TTY panel (`space` toggle, `a` all,
  `n` none, `q` done). Off a TTY, or with `on|off [name…]`, it prints or
  mutates the same homes. `/scope` is the REPL entry. `install-skills` still
  turns every home on. Disk is the source of truth; leftover does not draw a
  chat pager (`--tui` is still the vendor TUI).
- **Live activity on long `--print` turns.** ACP already parsed
  `agent_thought_chunk`, and `plan` / `plan_update` already arrived as
  protocol noise. Headless leftover only printed tool titles (`Read File`,
  `grep`), so a parent chat could not see what the worker was doing. Progress
  on stderr now includes the in-progress plan step, a compact thought line,
  tool paths when ACP sends locations, and a heartbeat that repeats the last
  activity. Streamed answer text still stays on stdout.
- **`leftover quota --json` and `leftover --why --json`.** Scriptable views of
  the windows and lag+waste scores leftover already prints. Same columns, no
  strength field, no tokens. `--why --json` is the table, not a pick dump.
- **Roadmap and D18.** Long-term growth is filtered: official remaining,
  lag+waste, or the parent conversation. Community-facing next work is listed
  in `docs/roadmap.md`; wrappers, proxies, and a second frontend are closed.
- **Skill table includes Antigravity.** `--agent antigravity` and the coding
  pool now name `agy --model gemini-3.1-pro-high` next to the other four CLIs.
- **Explicit turn handles and completion callbacks.** `AgentPool.submit()` now
  returns a one-shot `TurnHandle` with queued/running/terminal states,
  observational waits, explicit cancellation, a separate cleanup boundary,
  and bounded completion FIFOs isolated by parent id. Empty owner queues retire
  automatically; overflow counts and explicit owner cleanup remain observable.
  The existing `pool.run()` API still returns a `Turn` and does not retain
  callback entries.
- **Antigravity (`agy`) as a fifth backend**, last in the coding pool. Google's
  terminal agent has no ACP mode in 1.1.19, so it runs exec-only and never
  holds a sticky session. It is pinned to `--model gemini-3.1-pro-high`: `agy`
  also offers Claude Opus/Sonnet and GPT-OSS, and routing there would spend
  Antigravity's third-party allowance instead of the Google pool — the same
  rule Cursor has always had for `grok-4.6`.
  - `agy` exits 0 even when it fails; the JSON body carries `status: "ERROR"`
    and `error`, which the exec runner already classifies, so no shared code
    needed a vendor special case.
  - `--print-timeout 15m` is passed explicitly. Without it `agy` abandons the
    turn at its own 5-minute default while leftover is still waiting.
  - No vendor usage endpoint is known, so ranking uses the local ledger against
    `budget_5h_turns` / `budget_week_turns`. `/quota` says "no vendor number"
    rather than inventing one.
  - Known constraint: two concurrent `agy -p` runs share one background
    language server and cancel or cross-wire each other. leftover already
    serializes turns per agent, but a second leftover — or the Antigravity
    IDE open beside it — will break turns.
  - Known flakiness: on tool-using turns `agy` often reports the turn as
    failed *after* it has already applied its edits. Its own log shows
    `ReplaceFileContent` auto-approved and written, the file on disk is
    correct, and the envelope then comes back `status: "ERROR"` with
    `error: "context canceled"` at around 90 seconds. Two of three
    file-editing turns ended that way, including one run with nothing else
    touching `agy`, so this is not the concurrency issue above. leftover
    treats it as a failure and fails over, so a second backend redoes work
    that was already done. leftover is not special-casing it: the routing is
    correct given what the CLI reports, and swallowing a vendor's own error
    would hide real partial failures.

### Fixed

- **Claude and Antigravity quota/doctor remaining.** `leftover quota` and
  `leftover doctor` now always take a live snapshot. Claude's OAuth refresh
  sends the same `User-Agent: claude-code/…` the CLI uses, so Cloudflare no
  longer 403s the token endpoint as a browser. Extra credits are a percent
  window (still excluded from ranking headroom). A 0% window with no reset
  clock is no longer labelled "just reset". When a vendor is silent,
  estimated local remaining is drawn and labelled `estimated local` instead
  of `no vendor number`.
- ACP long sessions now register their terminal waiter before prompt execution,
  settle each prompt exactly once, and reject updates captured for an older
  prompt epoch. Timeout/cancellation becomes visible before abort cleanup, and
  an uncertain prompt still rotates the whole ACP generation because the
  protocol has no prompt id on updates.
- ACP event delivery now uses one ordered pump and publishes an immutable
  text/tool/error snapshot. Text queued before a backend failure is preserved,
  while failure and cancellation can reach the parent without waiting behind a
  blocked UI callback.
- Cancelling a foreground `pool.run()` no longer waits behind stubborn worker
  cleanup. The caller receives cancellation immediately while the same worker
  finishes process reaping and lock release in the background.
- Pool cancellation now covers queued work, direct startup/warmup tasks, and
  already-settled turns still in cleanup. Cancelled routes cannot restart on a
  fallback backend, and ACP sink timeouts settle before abort cleanup.
- Runner startup finalizers now exist before startup can yield, close partial
  transports immediately, and close again after a late startup becomes
  terminal. Timed-out cleanup remains owned without duplicate close attempts;
  failed or self-cancelled closes stay observable and retry on a later explicit
  shutdown. Pool deadlines also cannot trap `asyncio.run()` during teardown.
- Debate slots now use a hard observer deadline, so a backend coroutine that
  delays cancellation cannot postpone the judge or the parent result.
- The doctor roster and the `--why` table hardcoded an 8-to-10 character agent
  column, so an 11-character label ("Antigravity") broke both alignments. Both
  now size the column from the agents actually being shown.
- `leftover.example.toml` still carried a real account name in the `sub2api`
  comment, and its `[routing]` keys would have silently dropped the new backend
  out of the pool for anyone copying the file.

## 0.1.0 — 2026-08-23

First public snapshot. leftover is a thin local router: it classifies a task,
picks the logged-in subscription CLI whose quota window is most behind
schedule, and hands the work off — without becoming a second coding terminal.

### The product

- **lag+waste routing.** `lag` is how far behind schedule a window is;
  `waste` is the catch-up rate it needs before reset. `total = 0.5*lag +
  1.0*waste`. A fresh short window starts at zero urgency instead of starving
  an overdue weekly pool. Estimated turn budgets never contribute `waste`.
- **Four backends, official CLIs only.** `codex`, `grok`,
  `cursor-agent --model grok-4.6` in the coding pool; `claude` for `/plan` and
  as last-resort fallback. No proxy, no API keys, no model substitution.
- **One conversation.** A thin REPL over ACP sessions, `--print` for headless
  one-shots, `--tui` to hand the terminal to the winner's own UI.
- **Group modes.** `/rt` roundtable, `/all` broadcast, `/debate` (two argue, a
  third judges), `/relay` (plan → implement → review).
- **Quota views.** `leftover quota` renders each vendor's window against the
  calendar; `leftover --why` prints the lag+waste table behind a pick;
  `leftover doctor` shows the roster, versions, and remaining bars.
- **Skill ABI.** `leftover --pick --json --agent <self>` lets another agent ask
  where a task should go. `leftover install-skills` symlinks `SKILL.md` into
  the five CLI skill directories.
- **Failover that is never silent.** Refusals are classified (quota, auth,
  rate limit, transient), the backend is benched with the right cooldown, and
  the substitute is announced. Turn and idle timeouts are terminal: a
  possibly state-changing task is never replayed on a second vendor.

### Fixed before publishing

- `leftover doctor --config PATH` and `leftover quota --config PATH` silently
  ignored the flag and loaded the default config; a missing value raised
  `IndexError` instead of a usage error.
- `leftover quota` rendered every reset time in `Europe/London` regardless of
  where the machine was. The clock is now the machine's, with
  `[leftover] timezone` as an override.
- A refreshed Claude OAuth token was passed to `security add-generic-password`
  as an argv element, making it visible in `ps` for the duration of the call.
  It now goes in on stdin.

### Changed

- The `/quota` rhythm view is in English (`▾behind` / `▴ahead`,
  `widening` / `narrowing`, `calendar` / `used`), matching the rest of the CLI.
- `python-telegram-bot` moved to the optional `telegram` extra; the router
  itself installs with one dependency. `agora bot` explains the extra instead
  of raising `ImportError`.
- `LEFTOVER_TELEGRAM_TOKEN` replaces `AGORA_TELEGRAM_TOKEN`, which still works.
- `com.agora.bot.plist` became `com.leftover.bot.plist` and now launches the
  installed `agora bot` entry point rather than the module `agora`, which no
  longer exists. Reload it if you had the old label installed.
- Residual `agora` identity in code paths that other tools can see: the ACP
  `client_info` is now `leftover`, and loggers/threads are `leftover.*`.
- `agora.example.toml` was dropped; `leftover.example.toml` documents every
  key. `~/.config/agora/agora.toml` is still read as a fallback.

### Added

- `SECURITY.md`: every file each quota probe reads, every host it contacts,
  everything leftover writes, and the auto-approve flags each subagent gets.
- GitHub Actions CI on Linux and macOS, Python 3.10–3.13, plus `ruff`.
- `docs/demo.svg`, generated from real output by `scripts/demo_cast.py`.
