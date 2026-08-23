# Changelog

Notable changes per release. Dates are release dates.

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
