<div align="center">

# leftover

**Spend leftover Codex / Grok / Cursor quota before the window resets.**

[![ci](https://github.com/XR-Lee/leftover/actions/workflows/ci.yml/badge.svg)](https://github.com/XR-Lee/leftover/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

<img src="docs/demo.svg" alt="leftover doctor, leftover --why, leftover quota" width="820">

</div>

You pay for four coding subscriptions. Each one meters a window — 5 hours, a
week, a month — and every window you underuse is money that expires quietly.

```
leftover "migrate sessions onto JWT"
leftover · Cursor
```

A thin local router: classify a task, pick a logged-in official CLI by
lag+waste, stay in one parent conversation. Not a second TUI. Not a bot fleet.

Closest analog is [usher](https://github.com/theodorebeaupre-prog/usher) (launcher → official TUI). leftover does not rewrite that. `--tui` is that path. What usher does not do: official remaining quota, spend the rotting window, roundtable / debate / relay in one conversation.

`macbot` is a one-minor-version alias.

## Rules

| Task | Who |
|---|---|
| Default coding | Highest lag+waste among `codex`, `grok`, `cursor-agent --model grok-4.6`. A successful in-process coding session stays on its backend; independent commands re-rank. |
| `/plan` | Claude first. Coding pool if Claude is out. |
| `/cu` or “computer use / 点界面” | Codex CLI only. |
| `@codex` `@grok` `@cursor` `@claude` | Named beat scores. |

Cursor Ultra coding stays on first-party Grok. Do not send Cursor onto Claude/GPT models.

## Install

```bash
# official CLIs, already logged in: claude / codex / grok / cursor-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

leftover doctor
leftover install-skills
```

Copy `leftover.example.toml` to `~/.config/leftover/leftover.toml` if you want
overrides. Built-in four agents work with no config. The frozen Telegram
transport is an extra: `pip install -e '.[telegram]'`.

## Usage

You are talking to **leftover**. Codex / Grok / Cursor / Claude are subagents it spawns.

```bash
cd your-repo
leftover
you> swap session for JWT, keep tests green
you> /plan should we split the worker
you> /cu click through signup
you> /quit
```

| Command | What |
|---|---|
| (type) | coding, pick, continue in this conversation |
| `/plan …` | Claude plans |
| `/cu …` | Codex computer use |
| `/rt …` | roundtable |
| `/debate …` | two argue, third judges |
| `/relay …` | plan → implement → review |
| `/all …` | parallel, independent |
| `/quota` `/cd` `/reset` `/who` `/quit` | quota, workdir, reset, exit |
| `--tui` | exec into the winner's own UI (usher's path) |
| `-p` / `--print` | headless; stdout is the answer |
| `--why` | lag+waste table |
| `--pick --json` | skill ABI |

## Runtime guarantees

- ACP is preferred when available. Cursor and Codex keep their native ACP runner and session across explicit turns; exec is only a startup fallback or an explicit override.
- A full-turn or ACP-idle timeout ends that request and is never replayed on another backend. Fast startup, authentication, quota, and transient failures may still follow the configured fallback chain.
- Quiet `--print` routes report route/attempt/tool progress and a 30-second heartbeat on stderr. Stdout remains answer-only.
- Skill handoffs wait on the returned process handle; they never defer the next check from a model-generated duration estimate. If an exec leader exits while descendants retain its pipes, leftover reaps that process group and returns the captured answer immediately.
- `/all` workers and `/debate` advocates run in parallel, but each completed answer is emitted as one grouped block. Debate roles and event delivery have finite deadlines.
- Shutdown rejects work that was already queued and bounds transport cleanup. `/cd` waits for active work, then later operations start only in the new directory.

Seat line (usher-shaped, leftover axis):

```
→ Codex  (coding · lag+waste · override with @name)
```

## Score

- **lag** = `max(0, elapsed fraction − used fraction)`
- **waste** = `behind-schedule fraction / hours until reset`
- **total** = `0.5 * lag + 1.0 * waste`

A fresh short window starts at zero urgency. If it remains unused, its lag and catch-up rate rise; near reset it still beats a relaxed monthly pool. Estimated budgets have `waste = 0`.

## What it touches

leftover has no server, no account, and no telemetry. It reads the vendor logins
you already have to ask each vendor how much of *your* window is left, and it
starts every subagent with that CLI's permission prompts turned off.

Read [SECURITY.md](SECURITY.md) before you run it. It lists every file read,
every host contacted, and every auto-approve flag.

## Docs

| Doc | Use when |
|---|---|
| [Core features](docs/core-features.md) | What the product is |
| [Architecture](docs/architecture.md) | Module map |
| [Maintenance](docs/maintenance.md) | Invariants and tests |
| [Security](SECURITY.md) | What it reads, sends, writes |
| [Decisions](docs/history/decisions.md) | Why |

```bash
python tests/test_macbot.py
python tests/test_routing.py
python tests/test_e2e.py
python tests/test_reliability.py
python tests/test_state_reliability.py
```

No network, no vendor CLI, no subscription spend — the suites run on mocks.

MIT. Contributions: read [CONTRIBUTING.md](CONTRIBUTING.md) first — leftover
stays a thin router.
