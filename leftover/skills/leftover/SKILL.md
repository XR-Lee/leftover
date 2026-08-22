---
name: leftover
description: Route coding, planning, computer-use, roundtable, broadcast, debate, or relay work onto the right local subscription CLIs (Codex, Grok Build, Cursor Grok, Claude). Use when starting work, when unsure which agent should spend quota, when the user says leftover, macbot, /plan, /cu, /rt, /all, /debate, /relay, names multiple agents, or asks you to hand off to another CLI.
---

# leftover

You are talking to the human. leftover is the parent conversation. Codex / Grok Build / Cursor Agent / Claude Code are **subagents** it spawns (ACP harness: tools + session). They are not the thing the user is chatting with.

If this prompt already says you are a leftover subagent: **do the work now. Do not run `leftover` or `macbot`.**

Before doing a non-trivial coding, planning, computer-use, or multi-agent discussion task yourself, run:

```bash
leftover --pick --json --agent "$LEFTOVER_SELF" "$TASK"
```

If `leftover` is not on PATH, try `macbot --pick --json --agent "$LEFTOVER_SELF" "$TASK"` or `python3 -m leftover`.

`--agent` is **who you already are**. It is not an `@` mention and does not force routing.

| You are | `--agent` value |
|---|---|
| Codex CLI / Codex app / ChatGPT coding | `gpt` |
| Grok Build | `grok` |
| Cursor Agent CLI | `cursor` |
| Claude Code | `claude` |

`$TASK` is the user's request, unmodified.

## What to do with the JSON

- `run` is null: report `reason` and stop. The requested route is unavailable.
- `kind` is `computer_use` and `run` is present: hand off to Codex CLI (`gpt`) via `run` below. If you already *are* Codex, do the work with computer-use tools on this Mac. Do not send computer use to `grok`, `claude`, or `cursor-agent`.
- `kind` is `roundtable`, `broadcast`, `debate`, or `relay` and `run` is present: this is a leftover group run, not a task for one worker. Show `announce`, execute `run`, wait for every turn, and give the human its stdout. Do this even when an individual `agent` would otherwise match you.
- `agent` is null: report `reason` and stop.
- `agent` matches you (`gpt` if you are Codex, etc.): **do the work yourself**. Do not spawn another copy of yourself. Do not run `leftover --print`.
- `agent` is someone else: show `announce`, then run the `run` argv from the JSON (headless). Wait for it to finish. Give the human the stdout. If stderr has a `routed:` line, mention that quietly. **Do not exec `spawn`** — that is a TUI and the answer will not come back to this chat.
- Show the human exactly `announce` from the JSON, as one quiet line (example: `leftover · Cursor`). That is the routing entry — which subagent is doing this turn. Do not print `chain`, `reason`, arrows, or a preamble like "我来对齐". Never silently switch voices.

If `run` is missing, the equivalent is:

```bash
leftover --print --use <agent> "$TASK"
```

## User tags (do not guess)

- `/plan …` or "先出方案" → Claude first.
- `/cu` or "computer use" / "点界面" → Codex CLI (computer use on this Mac).
- `/rt`, `/all`, `/debate`, `/relay`, or multiple `@agent` mentions → run the requested multi-agent panel through leftover.
- `@codex` `@gpt` `@claude` `@grok` `@cursor` → that backend first.

Default with no tag is **coding**. Coding pool is Codex, Grok Build CLI, and `cursor-agent --model grok-4.6` (Cursor Ultra first-party). Claude is last-resort fallback, not in the lag race.

Do not send Cursor Agent onto Claude or GPT **models**. That spends the Cursor third-party pool, not those subscriptions.

Do not use reverse proxies or API keys to "unify" quota. Subscription windows only move when the official CLI/app of that vendor runs.
