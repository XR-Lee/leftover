# Roadmap

What leftover should grow, and what it should refuse. Why is
[D18](history/decisions.md). What must stay true is
[Core features](core-features.md).

leftover is not trying to become the best coding terminal. The long bet is:
**ask each vendor how much of your window is left, spend the one that is
rotting, stay in one parent conversation.** Everything else is either
maintenance of that bet or a distraction.

Closest analog is [usher](https://github.com/theodorebeaupre-prog/usher).
usher already ships the launcher, `--why`, and failover-into-the-vendor-TUI.
Its FAQ says vendors do not expose remaining quota. For Codex, Claude, Grok,
and Cursor that sentence is false — leftover already reads those official
numbers. That gap is the product.

## Filter

A change is on the roadmap only if it deepens one of:

1. **Official remaining** — `quota.py` asks the vendor. Degrade honestly.
2. **lag+waste** — spend the rotting window, not the “best” model.
3. **One parent conversation** — ACP REPL, `--print`, `/rt` `/debate` `/relay`.

It is off the roadmap if it is a second TUI, a reverse proxy, a hosted
account, a second chat frontend, a reputation/strength score, or a sixth
backend that cannot speak ACP/exec and cannot report remaining.

## Now — keep the moat from rotting

Vendor field names move. The GitHub community will judge leftover by whether
`leftover quota` and `leftover --why` still print a real number after the
next CLI update.

| Item | Why the community cares | Done when |
|---|---|---|
| Parser fixtures next to every `parse_*` | Outsiders can reproduce a probe without spending a subscription | each vendor has a recorded payload in tests; a field rename is a fixture + parser change, not a rewrite |
| Codex reported without Sub2API | Most operators have no admin key; `~/.codex/sessions` often stores `used_percent: null` | `probe_codex` has a second official read, or `/quota` says why Codex is estimated |
| Cursor token without the macOS IDE db | CI already runs Linux; no `state.vscdb` means the dashboard probe never fires | Linux/Windows CLI-only logins still reach `GetCurrentPeriodUsage`, or doctor says which file is missing |
| Antigravity reported | Fifth backend is in the coding pool (D17) but ranks on a local ledger | `quota_probe` exists. Until then `/quota` may show an `estimated local` percent; unlabeled vendor remaining is a bug |
| Probe contract in `SECURITY.md` | A tool that spends your windows converts on auditability | every new host/file is in the read/send/write tables in the same commit |
| Recorded-payload probe tests | “We really ask the vendor” must be demonstrable offline | no test hits a live vendor host; CI stays subscription-free |

These are not new product surfaces. They are the reason leftover is not
usher-with-a-different-name.

## Next — make the proof scriptable

Git users do not live in a REPL. They screenshot, pipe, and diff.

| Item | Why | Constraint |
|---|---|---|
| `leftover quota --json` | Machine-readable windows (`source`, `used_percent`, `resets_at`, `note`) | **shipped 2026-08-23.** same data `/quota` already shows; no new probe |
| `leftover --why --json` | The lag+waste table without the seat chrome | **shipped 2026-08-23.** columns stay lag / waste / total. No strength |
| Doctor “how this number was read” | Trust: host + local file, not a slogan | reuse `SECURITY.md` paths; do not dump tokens |
| Skill table includes Antigravity | D17 added `agy`; the skill still listed four backends | **shipped 2026-08-23.** `--agent antigravity`; do not send `agy` onto Claude/GPT models |

`--json` here is a view of numbers leftover already has. It is not a new
API product.

## Later — only if it stays a thin router

| Item | Why it might be worth it | Why it can wait |
|---|---|---|
| Vendor **plugin** wrapping `--pick` (D8 tier C: `/leftover` inside Grok or Codex) | People already sit in the official TUI; a slash is more natural than `install-skills` + a second REPL | Skill ABI already works. A plugin that edits vendor TUI source is D8-D and is rejected |
| Group-mode reliability | `/debate` and `/all` are a real blank vs OpenACP (one agent per session) and usher (leaves after exec) | Audience is narrow. Stability (deadlines, dirty-tree notice, grouped emit) beats a sixth mode |
| Honest estimated budgets | Claude/Cursor turn caps are still unknown; ledger only matters when the usage API fails | Do not let guessed budgets grow `waste` |
| Sixth backend | Only with ACP or headless print **and** a remaining probe or an honest estimate | Agy is the warning: exec-only + no vendor number is already the ceiling of what we will absorb |

## Community attention that is not a feature

These are the GitHub/HN openers. They are discipline, not code.

- **Not a proxy.** Official CLI + your login. Agent loop, sandbox, and tools
  stay vendor-owned. The first comment on this category is always “this will
  get your account banned”; leftover’s answer is the spawn line in
  `SECURITY.md`, not a new flag.
- **No server, no account, no telemetry.** The conversion asset is the
  file-by-file, host-by-host list. Keep it shorter than the router.
- **Do not pitch “thin router.”** usher already said that sentence. Repeating
  it classifies leftover as another wrapper.

## Will not do

Closed by D18. An issue that asks for one of these should be pointed here,
not put on a later milestone.

- Reverse-proxy a subscription as OpenAI-compatible completions
- Default the product to `--tui` (usher’s path; leftover’s default stays the parent conversation)
- Task-type × reputation, a strength column, or an animated banner
- Grow Telegram or add Discord / Slack
- Fork `xai-org/grok-build` or `openai/codex` to swap backends
- Route Cursor or Antigravity onto another vendor’s models
- Put Claude in the lag+waste race
- A leftover-drawn pager, sandbox, or subagent tree
- Telemetry, accounts, or a hosted quota service

## How a new idea gets in

1. Name which of the three bets it deepens.
2. If it is a new default, a new frontend, a new built-in agent, or a vendor
   plugin beyond `install-skills`, add a decision on top of
   [decisions.md](history/decisions.md).
3. If it is a probe, update `SECURITY.md` in the same commit and add a
   fixture-shaped parser test.
4. If it fails the filter, it belongs under **Will not do**, not in a drawer
   labelled “someday.”
