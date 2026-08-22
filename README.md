# leftover

Spend leftover Codex / Grok / Cursor quota before the window resets.

```
leftover "migrate sessions onto JWT"
leftover · Cursor
```

A thin local router: classify a task, pick a logged-in official CLI by lag+waste, stay in one parent conversation. Not a second TUI. Not a bot fleet.

Closest analog is [usher](https://github.com/theodorebeaupre-prog/usher) (launcher → official TUI). leftover does not rewrite that. `--tui` is that path. What usher does not do: official remaining quota, spend the rotting window, roundtable / debate / relay in one conversation.

`macbot` is a one-minor-version alias.

## Rules

| Task | Who |
|---|---|
| Default coding | Highest lag+waste among `codex`, `grok`, `cursor-agent --model grok-4.6`. Same cwd sticks until a hard refusal. |
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

Copy `leftover.example.toml` to `~/.config/leftover/leftover.toml` if you want overrides. Built-in four agents work with no config.

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

Seat line (usher-shaped, leftover axis):

```
→ Codex  (coding · lag+waste · override with @name)
```

## Score

- **lag** = `max(0, elapsed fraction − used fraction)`
- **waste** = `remaining fraction / hours until reset`
- **total** = `0.5 * lag + 1.0 * waste`

A fat monthly pool loses to a 5h window about to reset unused. Estimated budgets have `waste = 0`.

## Docs

| Doc | Use when |
|---|---|
| [Core features](docs/core-features.md) | What the product is |
| [Architecture](docs/architecture.md) | Module map |
| [Maintenance](docs/maintenance.md) | Invariants and tests |
| [Decisions](docs/history/decisions.md) | Why |

```bash
python tests/test_macbot.py
python tests/test_routing.py
python tests/test_e2e.py
python tests/test_reliability.py
python tests/test_state_reliability.py
```
