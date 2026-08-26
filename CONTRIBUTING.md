# Contributing

Read [docs/core-features.md](docs/core-features.md), [docs/roadmap.md](docs/roadmap.md), and [docs/maintenance.md](docs/maintenance.md) before changing behavior.

leftover is a thin router. Do not add a chat TUI, a proxy, or a second chat
product. `leftover scope` is the skill-install switch (D19), not a pager.

## Checks

```bash
python tests/test_macbot.py            # product contract
python tests/test_routing.py           # probes, classification, fallback policy
python tests/test_e2e.py               # group modes on a mock ACP agent
python tests/test_telegram.py           # Telegram privacy and safe HTML chunks
python tests/test_reliability.py       # deadlines, process trees, ledger
python tests/test_state_reliability.py # multi-process state merging
ruff check leftover tests scripts
```

These tests use mock agents. They must not call a logged-in subscription CLI,
and they must not touch the network. CI runs all of it on Linux and macOS for
Python 3.10 through 3.13.

## Invariants (fail the PR if broken)

- `--agent` is caller identity; `--use` forces routing.
- Coding pick uses lag+waste on the allocation window (weekly, else
  monthly). Estimated windows have `waste = 0`. A 5h window does not set
  `total` when a weekly/monthly window is live.
- `/cu` is Codex only.
- Cursor stays on `--model grok-4.6`; Antigravity stays on a first-party
  `gemini-*` model. Neither may be pointed at another vendor's models.
- `leftover --pick --json` `run` is `leftover --print …`, never a vendor TUI.
- Classified quota text is a failure, not an answer.
- `install-skills` writes symlinks. `leftover scope off` unlinks only leftover's own skill path.
- Public name is leftover. `macbot` is an alias only.
- A secret never travels as an argv element. Keychain writes go in on stdin.
- `/quota` renders in English; its clock is the machine's unless
  `[leftover] timezone` says otherwise.
- A new quota probe updates [SECURITY.md](SECURITY.md)'s read / send / write
  tables in the same commit.

New decisions go at the top of [docs/history/decisions.md](docs/history/decisions.md).
