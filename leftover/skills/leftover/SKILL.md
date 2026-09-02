---
name: leftover
description: Route leftover or macbot tagged work onto Codex, Grok Build, Cursor, Antigravity, or Claude. Use only when the user names leftover or macbot, or they ask /plan, /cu, /heavy, /rt, /all, /debate, /relay, name multiple agents, or ask to hand off to another CLI. Do not use when this prompt already says you are a leftover subagent, leftover --pick already ran this turn, leftover scope is off, or the user said not to use leftover routing.
---

# leftover

You are talking to the human. leftover is the parent conversation. Codex / Grok Build / Cursor Agent / Claude Code are **subagents** it spawns (ACP harness: tools + session). They are not the thing the user is chatting with.

Stop and do the user's task yourself if any of these is true:

- This prompt already says you are a leftover subagent, leftover heavy, or leftover already routed you.
- leftover --pick already ran in this turn, or its JSON already arrived.
- leftover --pick JSON has `scope.active` false or `run` null because leftover scope is off.
- The user told you not to use leftover routing.

Do not run `leftover` or `macbot` in those cases. Lead with the outcome; do not write leftover a status report.

Otherwise, before doing a non-trivial coding, planning, computer-use, or multi-agent discussion task yourself, run leftover --pick **once**, with a literal `--agent` from the table. Never leave `--agent` empty. Never expand an unset `$LEFTOVER_SELF` into a blank flag.

```bash
leftover --pick --json --agent grok "$TASK"
```

Replace `grok` with the row that matches who you already are.

This pick re-reads `leftover scope` from disk. Vendor CLIs may retain a skill
they discovered before it was switched off, or discover it through another
CLI's compatibility directory; the returned `scope` field is authoritative.

This pick is also a foreground command. If the execution tool returns a live
handle before the JSON arrives, immediately block on that same handle; do not
estimate when routing will finish and do not start a second pick.

If `leftover` is not on PATH, try `macbot --pick --json --agent grok "$TASK"` or `python3 -m leftover --pick --json --agent grok "$TASK"`, still using the table value.

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

- `scope.active` is `false`: leftover was switched off for this CLI after the
  skill was discovered. Do the user's task yourself in the current CLI. Do not
  show `announce`, do not execute `run`, and do not invoke leftover again.
- `agent` matches you (`gpt` if you are Codex, etc.): **do the work yourself**.
  Do not spawn another copy of yourself. Do not run `leftover --print`, even
  when `run` is present. `run` is null in this case so an older skill that
  stops on a null run cannot leftover --print you again.
- `run` is null: if `scope.active` is false or `agent` matches you, do the
  work yourself. Otherwise report `reason` and stop. Never reconstruct
  leftover --print.
- `kind` is `computer_use` and `run` is present: hand off to Codex CLI (`gpt`) via `run` below. If you already *are* Codex, do the work with computer-use tools on this Mac. Do not send computer use to `grok`, `claude`, or `cursor-agent`.
- `kind` is `roundtable`, `broadcast`, `debate`, `relay`, or `heavy` and `run` is present: this is a leftover group run (or a single heavy worker if only one CLI is installed), not a silent switch of voice. Show `announce`, execute `run`, wait for every turn, and give the human its stdout. Do this even when an individual `agent` would otherwise match you.
- `agent` is null: report `reason` and stop.
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
