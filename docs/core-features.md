# Core features

This is the maintenance inventory. If a change does not serve one of these features, it does not belong in the future repo.

leftover is the parent conversation. Codex / Grok Build / Cursor Agent / Claude Code are subagents it spawns. The user is not switching chat products.

## What it is

A local `leftover` CLI that:

1. Classifies a line (`coding` / `plan` / `computer_use` / group modes).
2. Picks a logged-in official CLI by lag+waste, then falls back on classified refusal.
3. Either stays in a thin REPL (ACP session), prints headless, or `exec`s the vendor TUI.

It does not draw a pager, sandbox, diff viewer, or subagent tree. Those stay inside the vendor CLI.

## User and skill surface

| Feature | How it is invoked | Owner | Tests |
|---|---|---|---|
| Parent REPL | `macbot` then type; optional first argv | `leftover.chat` | compose, pick, group routes |
| Headless one-shot | `leftover --print` / `-p "…"` | `run_print` / `run_discuss` | `test_run_print_*`, stdin replay |
| Headless JSON | `leftover -p --json "…"` | run envelope | `test_print_json_envelope_and_stdin` |
| Headless timeout | `leftover -p --timeout 2m "…"` | per-attempt; exit 124 | parse + requires `-p` |
| Skill handoff | `leftover --pick --json --agent $SELF "…"` | `decide`, `Pick.as_dict` | `--agent` vs `--use`, group `run` argv |
| Preview | `macbot --dry-run "…"` | `_print_pick` | pick chain |
| Why table | `macbot --why "…"` | `format_why` | lag/waste + remaining bar; no strength |
| Seat / failover | TTY stderr/stdout | `ui.seat_line` / `failover_line` | usher-shaped, our axis |
| Vendor TUI | `macbot --tui "…"` | `_exec` / `spawn_argv` | ACP command pins |
| Plan | `/plan` or `--plan` | intent + `plan_key=claude` | `test_pick_plan_and_cu` |
| Computer use | `/cu` or `--cu` or explicit “点界面” | always Codex (`gpt`) | same + no fallback past Codex |
| Named backend | `@codex` `@grok` `@cursor` `@claude` | `intent.named` | intent + `--use` |
| Group: roundtable | `/rt` or two+ `@` mentions | `orchestrator._run_sequence` | group pick JSON + e2e |
| Group: broadcast | `/all` | `_run_parallel` | e2e |
| Group: debate | `/debate` (needs 3 CLIs) | `_run_debate` | `test_debate_is_parallel_and_compact` |
| Group: relay | `/relay` plan→implement→review | `_run_relay` | e2e |
| Quota view | `leftover quota` or `/quota` | `router.report` + `rhythm` | parse probes + rhythm |
| Doctor | `leftover doctor` | `doctor` | roster + remaining bar + paths |
| Install skill | `leftover install-skills` | symlink `SKILL.md` | `test_skill_install_is_symlink` |
| Sticky cwd | same directory keeps last success | `leftover-state.json` | quota cache + pick |
| Workdir | `/cd` in REPL; `--print` uses `os.getcwd()` | `AgentPool.set_workdir` | `test_run_print_uses_current_workdir` |

## Routing that must stay true

| Rule | Why |
|---|---|
| Default task is coding | Claude is not in the lag race |
| Coding pool = `gpt`, `grok`, `cursor` | Cursor pinned to `--model grok-4.6` (Ultra first-party) |
| Score = `0.5 * lag + 1.0 * waste` | A 5h window about to reset beats a fat monthly pool |
| `waste = 0` on `estimated` | Turn budgets must not fake a 5h emergency |
| `/plan` → Claude first, coding pool as fallback | Planning is the only default Claude job |
| `/cu` → Codex only, no further fallback | Computer use is a Codex harness, not a model id |
| `--agent` is caller identity | Must not force routing (skill anti-recursion) |
| `--use` / `@name` forces first backend | Explicit beat scores |
| Same cwd sticks until hard refusal | Voice continuity without a second frontend |
| Substitution is never silent | `routed:` on stderr / dim line in REPL |
| Failover gets a dirty-tree notice | `continuation_guard` (usher); toml can turn it off |
| Group `--pick` returns `run: leftover --print /rt @…` | Skills must not `exec` a TUI |

## Harness rules

| Rule | Owner |
|---|---|
| Official CLI only. No reverse-proxy completions | D1 |
| ACP first, exec if ACP missing or start fails | `AgentPool`, D3 |
| First turn injects WORK/PLAN_ONLY; follow-up is the bare user line | `_compose` |
| Subagent prompt forbids re-entering `leftover` | WORK / PLAN_ONLY |
| Refusal classified in the router, including short *result text* | D4, 300-char scan |
| Benched agent is not called | circuit breaker |
| Tool permissions auto-approved | exec/ACP flags in `config.BUILTIN_AGENTS` |

## Quota probes (reported → observed → estimated)

| Agent | Reported | Fallback |
|---|---|---|
| Codex (`gpt`) | Sub2API admin `/accounts/:id/usage` if configured, else `~/.codex/sessions` | observed refusal |
| Claude | `GET /api/oauth/usage` with the CLI's own OAuth | observed (`You've hit your weekly limit` as body) |
| Grok | CLI-proxy `/v1/billing`; live ACP `x.ai/billing` only if already connected | local signals, then estimated |
| Cursor | dashboard `GetCurrentPeriodUsage` via IDE `state.vscdb` token | plan name is not remaining quota |

Probes reuse the vendor login. They must not spawn a Grok ACP session just to read billing, and must not hit grok.com gRPC-web.

## Not a core feature — do not grow

| Thing | Where | Why it stays frozen |
|---|---|---|
| Telegram bot | `transports/telegram.py`, `render.py`, `com.agora.bot.plist` | D7: not the main path. D6 still pending. |
| `agora console` | `transports/console.py` | Duplicate of `leftover` REPL |
| Fork / wrap Grok or Codex TUI | — | D8: vendors do not take PRs; harness is vendor-owned |
| Reverse proxy / OpenAI-compatible gateway | — | D1: loses the agent loop, ToS risk |
| Cursor on Claude/GPT models | — | Spends Cursor third-party pool |
| Guessing computer-use from generic text | `intent.py` | Only `/cu` and the explicit phrases |
| leftover-drawn fullscreen UI | `ui.py` is chrome only | Want a pager → `--tui` |

`rhythm.py` is core (it is `/quota`). `render.py` is leftover (Telegram HTML).

## Size (so growth is visible)

Approximate lines, 2026-08-22:

| Layer | Files | Lines | Role |
|---|---|---|---|
| CLI + intent + score + ui | `leftover` `intent` `score` `ui` | ~1.1k | product |
| Quota + router + rhythm | `quota` `router` `rhythm` | ~2.3k | pick / fallback |
| ACP/exec pool | `agents/*` | ~0.7k | harness |
| Group modes | `orchestrator` `transcript` | ~0.5k | `/rt` `/all` `/debate` `/relay` |
| Config / doctor | `config` `doctor` | ~0.4k | defaults |
| Leftover Telegram | `telegram` `render` `console` | ~0.5k | freeze |
| Tests | `tests/test_*.py` | ~2.0k | contract |

`quota.py` is the hotspot. New vendor field names belong there plus a parser test, not a new abstraction.
