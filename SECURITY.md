# Security and privacy

leftover runs entirely on your machine and reuses logins you already have. It
has no server, no account, and no telemetry. It also touches every vendor CLI's
local credential store, so here is the complete list of what it reads, what it
sends, and what it writes.

## What leftover reads

Quota probing is the only reason any of this is read. Each probe is best-effort
and degrades to "no vendor number" instead of failing a route.

| Vendor | Read from |
|---|---|
| Claude | `$CLAUDE_CODE_OAUTH_TOKEN`, `$CLAUDE_CONFIG_DIR/.credentials.json` or `~/.claude/.credentials.json`, macOS Keychain item `Claude Code-credentials`, `~/.claude.json` (plan name), `~/.claude/usage-limits.json` (ccusage cache, if present) |
| Codex | `~/.codex/sessions/**/*.jsonl` — tail of recent rollout logs, only the `rate_limits` records |
| Grok | `~/.grok/auth.json` (the SuperGrok OIDC session, API keys are skipped), `~/.grok/sessions/*/signals.json` |
| Cursor | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` opened read-only for `cursorAuth/accessToken` and `cursorAuth/stripeMembershipType`; Keychain item `cursor-access-token`; `cursor-agent about` |

These are the same credentials the vendor CLIs use. leftover does not create
logins, does not ask for API keys, and never copies a token anywhere except back
into the store it came from (see below).

## What leaves your machine

Only quota reads, each authenticated with that vendor's own token, each to that
vendor's own host:

| Request | Host |
|---|---|
| Claude usage | `https://api.anthropic.com/api/oauth/usage` |
| Claude OAuth refresh (only when the access token expired) | `https://platform.claude.com/v1/oauth/token`, falling back to `https://console.anthropic.com/v1/oauth/token` |
| Grok billing | `https://cli-chat-proxy.grok.com/v1/billing`, `/v1/settings`, `https://auth.x.ai` token exchange |
| Grok billing over a session already open | ACP extension method `x.ai/billing` |
| Cursor usage | `https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage` |
| Codex 5h/7d, only if you configured it | your own Sub2API `base_url` |

Your prompts go where you would expect: into the official CLI leftover spawns,
which talks to its own vendor. leftover adds no proxy and no third party.

The one exception is opt-in: the frozen Telegram transport (`agora bot`,
installed only with `pip install 'leftover[telegram]'`) talks to Telegram with
the bot token you supply, and relays messages for the user ids you list in
`[telegram].allowed_user_ids`. If you never install that extra, no such
connection exists.

## What leftover writes

| Path | Contents |
|---|---|
| `~/.local/share/leftover/leftover-state.json` | last route per directory, circuit-breaker health, last quota snapshot |
| `~/.local/share/leftover/ledger.json` | turn counts used for estimated budgets |
| `~/.local/share/leftover/history` | REPL readline history |
| `~/.claude/.credentials.json` (mode `0600`) or Keychain `Claude Code-credentials` | a refreshed Claude OAuth token, written back to whichever store it was read from |
| `~/.codex/skills/leftover/`, `~/.claude/skills/…`, `~/.agents/skills/…`, `~/.grok/skills/…`, `~/.cursor/skills/…` | leftover skill symlink; `leftover install-skills` / `leftover scope on` create it, `leftover scope off` removes only that leftover path |

Nothing is written anywhere else, and nothing is uploaded.

## Auto-approved tool use

This is the sharpest edge, and it is deliberate: leftover is an unattended
router, so it starts every subagent with that vendor's permission prompts
turned off.

```
claude       -p --dangerously-skip-permissions
codex        exec --dangerously-bypass-approvals-and-sandbox
grok         --always-approve --permission-mode bypassPermissions
cursor-agent -p --force
agy          -p --dangerously-skip-permissions
```

Over ACP, `request_permission` is auto-accepted the same way.

A routed subagent can therefore read, write, and delete files and run commands
in the working directory without asking. Run leftover in repositories you would
let an agent loose in, and prefer a workspace under version control. Every one
of these flags lives in `config.BUILTIN_AGENTS` and can be overridden in
`~/.config/leftover/leftover.toml` if you want the prompts back.

## Reporting a vulnerability

Open a GitHub issue for anything already public. For something that should not
be public yet, use GitHub's private vulnerability reporting on this repository
instead of an issue.
