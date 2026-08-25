---
name: leftover
description: Route coding, planning, computer-use, roundtable, broadcast, debate, or relay work onto the right local subscription CLIs (Codex, Grok Build, Cursor Grok, Antigravity, Claude). Use when starting work, when unsure which agent should spend quota, when the user says leftover, macbot, /plan, /cu, /rt, /all, /debate, /relay, names multiple agents, or asks you to hand off to another CLI.
---

# leftover

You are talking to the human. leftover is the parent conversation. Codex / Grok Build / Cursor Agent / Claude Code are **subagents** it spawns (ACP harness: tools + session). They are not the thing the user is chatting with.

If this prompt already says you are a leftover subagent: **do the work now. Do not run `leftover` or `macbot`.**

Before doing a non-trivial coding, planning, computer-use, or multi-agent discussion task yourself, run:

```bash
leftover --pick --json --agent "$LEFTOVER_SELF" "$TASK"
```

This pick is also a foreground command. If the execution tool returns a live
handle before the JSON arrives, immediately block on that same handle; do not
estimate when routing will finish and do not start a second pick.

If `leftover` is not on PATH, try `macbot --pick --json --agent "$LEFTOVER_SELF" "$TASK"` or `python3 -m leftover`.

`--agent` is **who you already are**. It is not an `@` mention and does not force routing.

| You are | `--agent` value |
|---|---|
| Codex CLI / Codex app / ChatGPT coding | `gpt` |
| Grok Build | `grok` |
| Cursor Agent CLI | `cursor` |
| Antigravity CLI | `antigravity` |
| Claude Code | `claude` |

`$TASK` is the user's request, unmodified.

## What to do with the JSON

- `run` is null: report `reason` and stop. The requested route is unavailable.
- `kind` is `computer_use` and `run` is present: hand off to Codex CLI (`gpt`) via `run` below. If you already *are* Codex, do the work with computer-use tools on this Mac. Do not send computer use to `grok`, `claude`, or `cursor-agent`.
- `kind` is `roundtable`, `broadcast`, `debate`, `relay`, or `heavy` and `run` is present: this is a leftover group run (or a single heavy worker if only one CLI is installed), not a silent switch of voice. Show `announce`, execute `run`, wait for every turn, and give the human its stdout. Do this even when an individual `agent` would otherwise match you.
- `agent` is null: report `reason` and stop.
- `agent` matches you (`gpt` if you are Codex, etc.): **do the work yourself**. Do not spawn another copy of yourself. Do not run `leftover --print`.
- `agent` is someone else: show `announce`, then run the `run` argv from the JSON (headless). Wait for it to finish. Give the human the stdout. If stderr has a `routed:` line, mention that quietly. **Do not exec `spawn`** — that is a TUI and the answer will not come back to this chat.
- Show the human exactly `announce` from the JSON, as one quiet line (example: `leftover · Cursor`). That is the routing entry — which subagent is doing this turn. Do not print `chain`, `reason`, arrows, or a preamble like "我来对齐". Never silently switch voices.

### Waiting for a handoff

`run` is a foreground command. Its process exit is the completion signal;
leftover cannot push a callback into this parent conversation. The JSON
`completion` field records this contract.

- Start `run` in the foreground. Do not detach it with `&`.
- If the execution tool says the command is still running and returns a handle
  (`session_id`, `cell_id`, or job id), immediately invoke the native blocking
  wait on that **same** handle. In Codex, use empty `write_stdin` calls for a
  returned `session_id`, or `wait` for a yielded exec cell. These waits return
  early when the process exits. If a host only offers non-blocking polling,
  poll at least every `completion.max_poll_interval_seconds` (10 seconds).
- Never choose the next poll from your estimate of task duration, and never
  sleep for one to five minutes before checking. The worker may have already
  finished. Do not start a duplicate `run` while its handle is live.
- stderr route, tool, plan/thought, failover, and `still working` lines are progress only.
  stdout is the answer. Keep waiting through quiet polls; treat process exit as
  success/failure and then return captured stdout to the human.

Do not synthesize a command when `run` is null or missing. That response has no
runnable handoff; report `reason` and stop. In particular, never use `spawn` or
reconstruct `leftover --print` after a handoff handle has already been returned.

## User tags (do not guess)

- `/plan …` or "先出方案" → Claude first.
- `/cu` or "computer use" / "点界面" → Codex CLI (computer use on this Mac).
- `/heavy`, `/discuss`, "should we", "一起写", "该不该", or a `?` → leftover heavy: local multi-model collab. Grok is the leader. Independent takes and compare-notes run in parallel.
- `/rt`, `/all`, `/debate`, `/relay`, or multiple `@agent` mentions → run the requested multi-agent panel through leftover.
- `@codex` `@gpt` `@claude` `@grok` `@cursor` `@antigravity` → that backend first.

Default with no tag is **coding**. Coding pool is Codex, Grok Build CLI, `cursor-agent --model grok-4.6` (Cursor Ultra first-party), and `agy --model gemini-3.1-pro-high` (Antigravity first-party). Claude is last-resort fallback, not in the lag race.

Do not send Cursor Agent or Antigravity onto Claude or GPT **models**. That spends their third-party pools, not those subscriptions.

Do not use reverse proxies or API keys to "unify" quota. Subscription windows only move when the official CLI/app of that vendor runs.
