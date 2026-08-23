# Contributing

Read [docs/core-features.md](docs/core-features.md) and [docs/maintenance.md](docs/maintenance.md) before changing behavior.

leftover is a thin router. Do not add a TUI, a proxy, or a second chat product.

## Checks

```bash
python tests/test_macbot.py            # product contract
python tests/test_routing.py           # probes, classification, fallback policy
python tests/test_e2e.py               # group modes on a mock ACP agent
python tests/test_reliability.py       # deadlines, process trees, ledger
python tests/test_state_reliability.py # multi-process state merging
ruff check leftover tests scripts
```

These tests use mock agents. They must not call a logged-in subscription CLI,
and they must not touch the network. CI runs all of it on Linux and macOS for
Python 3.10 through 3.13.

## Invariants (fail the PR if broken)

- `--agent` is caller identity; `--use` forces routing.
- Coding pick uses lag+waste; estimated windows have `waste = 0`.
- `/cu` is Codex only.
- Cursor stays on `--model grok-4.6`.
- `leftover --pick --json` `run` is `leftover --print …`, never a vendor TUI.
- Classified quota text is a failure, not an answer.
- `install-skills` writes symlinks.
- Public name is leftover. `macbot` is an alias only.
- A secret never travels as an argv element. Keychain writes go in on stdin.
- `/quota` renders in English; its clock is the machine's unless
  `[leftover] timezone` says otherwise.
- A new quota probe updates [SECURITY.md](SECURITY.md)'s read / send / write
  tables in the same commit.

New decisions go at the top of [docs/history/decisions.md](docs/history/decisions.md).
