# 四家订阅的技术底细

2026-08-21 调研。凡是标「未实测」的，用 `agora doctor` 或手动跑一次确认。

## 大前提

**订阅额度只能通过各家官方 CLI 的 OAuth 登录使用。** OpenRouter、LiteLLM、Cherry智能体聚合器走的都是 API key，碰不到订阅额度。所以任何"统一接口"方案的地基都是这四个 CLI。

## 入口表

| 订阅 | CLI | 安装 | headless | ACP |
|---|---|---|---|---|
| Claude Pro / Max | `claude` | `npm i -g @anthropic-ai/claude-code` | `claude -p --output-format json` | `npx @agentclientprotocol/claude-agent-acp`（v0.70+，**已实测可用**）。旧的 `@zed-industries/claude-code-acp` 停在 0.16.2 |
| ChatGPT Plus / Pro | `codex` | `npm i -g @openai/codex` | `codex exec --json` | `npx -y @agentclientprotocol/codex-acp@1.6.2`（ACP Registry 适配器，已实测双轮同 session） |
| SuperGrok / X Premium+ | `grok` | `curl -fsSL https://x.ai/cli/install.sh \| bash` | `grok -p`，支持 `streaming-json` | `grok agent stdio`（未实测，但 grok-telegram-bot 用的就是这条） |
| Cursor | `cursor-agent` | `curl https://cursor.com/install -fsS \| bash` | `-p`，`--list-models` | `cursor-agent --model grok-4.6 acp`（已实测双轮同 session） |

四个都吃 MCP。Claude / Codex / Grok Build 都声明支持 ACP。

Grok Build 目前是早期 beta，只对 SuperGrok 和 X Premium+ 开放，模型 slug `grok-build-0.1`（替代了废弃的 `grok-code-fast-1`），配置在 `~/.grok/config.toml`。

## ACP 和 MCP 的分工

- **MCP** = 给 agent 加工具。agent 是客户端，工具是服务端。
- **ACP** = 给 agent 换前端。你的程序是客户端，agent 是服务端，走 stdio 上的 JSON-RPC。

要「在自己的 UI 里驱动别人家的 agent」，要的是 ACP 不是 MCP。

Python SDK：`pip install agent-client-protocol`（用到的是 0.12.1）。

**踩过的两个坑：**

1. `run_agent(agent, input_stream, output_stream)` 的参数名是反的——`input_stream` 要传 **StreamWriter**，`output_stream` 要传 **StreamReader**。直接 `await run_agent(agent)` 让它自己建 stdio 最省事。
2. `on_connect` 是**同步调用**的。定义成 `async def` 会产生一个永远不被 await 的协程，连接对象拿不到，整个连接静默失败。

客户端最小流程：
```python
async with spawn_agent_process(bridge, *cmd) as (conn, proc):
    await conn.initialize(protocol_version=PROTOCOL_VERSION, ...)
    session = await conn.new_session(cwd=workdir, mcp_servers=[])
    await conn.prompt(session_id=session.session_id, prompt=[text_block(text)])
```
文本从 `session_update` 回调里的 `agent_message_chunk` 拿。

## 额度信号从哪读

三档，可信度递减。撞墙前的数字一律来自**那一家官方 CLI 已经登录过的账号**，打的是那一家自己的 usage 接口，不是反代，也不是把 completion 接到我们身上。

### 一档：厂商已经给出剩余额度

**Codex** — `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`，找 `token_count` 事件：

```json
{"payload": {"type": "token_count", "rate_limits": {
  "primary":   {"used_percent": 42.5, "window_minutes": 300,   "resets_in_seconds": 3600},
  "secondary": {"used_percent": 88.0, "window_minutes": 10080, "resets_in_seconds": 200000}}}}
```

primary = 5 小时窗口，secondary = 周窗口。注意 [openai/codex#14728](https://github.com/openai/codex/issues/14728)：**`codex exec` 模式下 `rate_limits` 可能是 null**，TUI / VS Code 模式才填。窗口是账号级的，所以取任何一个会话文件里最新的非空读数都有效。

**Grok Build** — 优先官方 CLI 自己的 REST：`GET https://cli-chat-proxy.grok.com/v1/billing?format=credits`，Bearer 来自 `~/.grok/auth.json` 的 OIDC session。SuperGrok 是**周窗**（`USAGE_PERIOD_TYPE_WEEKLY` + `creditUsagePercent`）。没有活着的 ACP 连接时不要再 spawn `grok agent stdio` 去调 `x.ai/billing`——stdio 面上这个方法经常是 Method not found，还会卡住选人 12 秒。

活着的 Grok ACP 会话仍可走扩展方法 `x.ai/billing`：

```json
{"billingCycle": {"billingPeriodEnd": "..."},
 "monthlyLimit": {"val": 99900}, "usage": {"totalUsed": {"val": 12345}}}
```

本地还有 `~/.grok/sessions/*/signals.json`（`contextTokensUsed`、`totalTokensBeforeCompaction`）——只是活动量，不是套餐上限。

不要去调 `grok.com` 那个 gRPC-web 计费端点。CLI-proxy `/v1/billing` 是官方 CLI 自己走的接口。

**Claude Code** — 和 `/usage` 同一条：`GET https://api.anthropic.com/api/oauth/usage`，`anthropic-beta: oauth-2025-04-20`。macOS token 在 Keychain `Claude Code-credentials`，Linux 在 `~/.claude/.credentials.json`。响应里现在是 `limits[]`（`session` / `weekly_all` / `weekly_scoped`），旧的 `five_hour` / `seven_day` 扁平成了 null。没有 `claude usage` 子命令。

**Cursor** — `cursor-agent about` 只有计划名。剩余额度走 dashboard：`POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage`，token 来自 Cursor IDE `state.vscdb` 的 `cursorAuth/accessToken`（CLI Keychain `cursor-access-token` 作备选）。读 `planUsage.totalSpend / limit`，重置时间是 `billingCycleEnd`（毫秒）。这是 Cursor 自己的未文档化 dashboard 接口，形状会变；变了就退回「只有计划名」。

### 二档：它拒绝了，并说了何时回来

Claude Code 的原文（来自官方错误参考）：

```
You've hit your session limit · resets 3:45pm
You've hit your weekly limit · resets Mon 12:00am
You've hit your Opus limit · resets 3:45pm
spend limit reached (daily; resets 2026-08-09 00:00 UTC)
API Error: Request rejected (429) · this may be a temporary capacity issue.
```

**最重要的一个坑：这些是当作普通回答正文返回的，不是 error。** `claude -p --output-format json` 里它出现在 `result` 字段。任何不做分类的封装都会把它当答案打进聊天窗口。

相关环境变量：`CLAUDE_CODE_MAX_RETRIES`（默认 10）、`CLAUDE_CODE_RETRY_WATCHDOG=1`（无人值守时对 429/529 无限重试）、`API_TIMEOUT_MS`（默认 600000）。

四种可解析的重置时间写法：`resets 3:45pm` / `resets Mon 12:00am` / `resets 2026-08-09 00:00 UTC` / `try again in 90 seconds`。

### 三档：谁都不说，只能自己数

usage 接口读不到时（Keychain 拒读、token 过期、dashboard 改字段），退回本地 turn budget。这不再是 Claude / Cursor 的默认态。

**诚实的差距：四家现在都能在撞墙前给数字**——前提是本机已经用官方 CLI/app 登录过。读失败时第一个硬信号仍然是拒绝，所以自动降级还是骨干。

## 其他

- macOS 自带 `python3` 常常是 3.9，`tomllib` 要 3.11+。`brew install python@3.12` 或者装 `tomli`。
- `launchd` 起服务时 PATH 是干净的，四个 CLI 会全部找不到。plist 里必须显式写 PATH。
- ccusage 能读 Claude Code 和 Codex 的本地日志算 token 用量，但**不报限流窗口**，只报 token 总数和成本。
