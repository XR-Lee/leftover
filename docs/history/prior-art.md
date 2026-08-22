# 现成方案对照

2026-08-21 调研，2026-08-22 补上 usher。**动手写任何东西之前先看这一页。**

## 一句话结论

最近的同类不是 acpbot，是 [usher](https://github.com/theodorebeaupre-prog/usher)（MIT，Go，v0.3）。它已经把「一条命令、官方 CLI、不走 API key、撞墙换人、exec 进自家 TUI」做完了。

D6 当时对照的是聊天桥（acpbot / OpenACP）和 completion 网关（OmniRoute），看错了类。usher 2026-07-11 就定了 launcher 规格，MacBot 8 月才写。命名提案里把它当品牌墙，没当该偷的架构——同一类错犯了两次。

这边还值得自研的只有 usher **故意不做** 的三件：

1. **读官方 remaining**（不是目击 cap 再衰减）
2. **lag+waste**（不是任务类型 × 口碑权重）
3. **同一场父对话**（ACP REPL、圆桌、辩论、接力）

启动器本身不要再写一遍。`--tui` 就是 usher 那条路。

## 对照表

| 项目 | 覆盖什么 | 不覆盖什么 | 许可 / 栈 |
|---|---|---|---|
| [usher](https://github.com/theodorebeaupre-prog/usher) | 检测已装 CLI → 任务类型×口碑×目击额度 → `--why` 表 → exec 官方 TUI；`-p` 无头 + 撞墙换人 + continuation notice；adapter 一个文件 | 父对话；官方 remaining；lag+waste；Grok Build；圆桌。FAQ 写「厂商不暴露剩余额度」——对我们四家是错的，见 D9 | MIT，Go 单二进制 |
| [acpbot](https://acpbot.app/) | Telegram + ACP。支持的正好是 `grok agent stdio`、`claude-agent-acp`、`codex-acp`、OpenCode。Telegram 话题当会话，权限按钮、媒体、定时 | 多 agent 同会话；额度感知；自动降级 | MIT，有 macOS/Linux 二进制和 Docker |
| [OpenACP](https://github.com/Open-ACP/OpenACP) | Telegram / Discord / Slack 三平台。通过 ACP Registry 支持 28+ agent。token 计量、每会话月度预算、审批按钮、自动放行规则 | **明确是一个会话一个 agent**，`/switch` 切换但不并存。无 agent 间辩论、无自动 fallback、无熔断 | MIT，TypeScript。自称早期阶段，小版本间会有破坏性变更 |
| [OmniRoute](https://github.com/diegosouzapw/OmniRoute) | 四层降级：订阅 → API key → 便宜模型 → 免费。按其自述用 14 个因子打分（health、quota、cost、latency、success rate、freshness…），带熔断器 | 是 OpenAI 兼容**网关**，出的是 completion 不是 agent run。见下方警告 | MIT |
| [zen-mcp / pal-mcp 的 clink](https://github.com/BeehiveInnovations/zen-mcp-server/blob/main/docs/tools/clink.md) | 在一个 CLI 里调另一个 CLI。spawn 本地已登录的二进制，所以走订阅不走 API key。自带 `consensus` 可以给不同模型分配正反立场 | 聊天前端；额度；持久服务 | MIT。装在 Claude Code 里当 MCP server |
| [Zed 外部 agent](https://zed.dev/docs/ai/external-agents) | 一个 IDE 里并排挂多个 ACP agent，各自用各自的登录，侧栏并行多线程 | 它们只是并排，不互相对话。非聊天场景 | 商业软件，agent 本身各自计费 |
| [artickc/grok-telegram-bot](https://github.com/artickc/grok-telegram-bot) | Telegram 控制 Grok Build，走 ACP stdio。会话持久化、流式 diff、多账号、24/7 服务 | 只有 Grok 一家 | TypeScript + grammY |
| [CLIProxyAPI](https://github.com/QuiteBitter/CLIProxyAPI) | 把 Gemini CLI / Codex / Claude Code / Qwen 的 OAuth 登录包成 OpenAI 兼容端点，多账号负载均衡 | 见下方警告。不支持 Grok 和 Cursor | — |

## 两个必须知道的警告

**OmniRoute 的订阅层大概率是反代。** 它的 Provider Reference 里 Claude Code 和 Codex 都标成 `OAuth`，没说是 spawn CLI 二进制。一个从 Claude Code 的 OAuth 登录里产出 OpenAI 兼容 completion 的网关，走的就是反代那条路——**给你的是裸模型补全，没有 agent loop、没有工具、没有文件访问**，而且违反各家 ToS。对纯对话有用，对"重活"完全没用。采用前自己去确认它到底怎么拿的 token。

**反代路线本身在这个场景里是负收益。** 看起来是通往统一接口的捷径，实际上把 Claude Code 的工具循环、Codex 的沙箱执行、Grok Build 的 subagent 全丢了。编程那一半的价值几乎全在 agent loop 里。

## 不要套的两家开源 TUI

「把 MacBot 做成 Grok/Codex terminal 的套壳」看起来能白嫖 fullscreen TUI。两边源码都是 Apache-2.0，但都不是壳：

| | [xai-org/grok-build](https://github.com/xai-org/grok-build) | [openai/codex](https://github.com/openai/codex) |
|---|---|---|
| 是什么 | pager（TUI）+ shell（agent runtime）+ tools，monorepo 定期 dump | TUI + core + app-server，周更 |
| 扩展点 | skills / plugins / hooks / custom **completion** models / ACP server | skills / plugins / MCP / ACP；**不收外部 PR** |
| 贡献 | CONTRIBUTING：不接受外部贡献 | 同上 |
| 绑死的 | `x.ai/*` ACP 扩展、worktree、billing、subagent UI | Codex 沙箱、computer use、ChatGPT 登录 |

Grok 的 `[model.*]` custom model 接的是 Chat Completions / Responses / Anthropic Messages，不是「把 Codex CLI 接进来」。那条路就是 D1 的反代，编程价值会掉光。

可维护的套法只有 **plugin/skill 挂进官方 TUI**（MacBot 已经是这个：`install-skills`），以及 **`exec` 进官方 TUI**（`--tui`）。fork pager 当自己的前端，见 `decisions.md` D8。

## 空白在哪（2026-08-22 更正）

**Launcher + 撞墙换人已经有人做了。** usher 就是。D6 写「没找到 CLI 层面的 agent 降级」时漏了它。

usher 明确放弃的，才是空白：

| 空白 | 谁已经做了 | 这边还要不要 |
|---|---|---|
| 选人、exec 进官方 TUI、`--why`、无头 failover | usher | 不要重写。`--tui` / `--why` / continuation_guard 对齐它 |
| 官方 remaining（OAuth usage / billing / dashboard） | 无。usher FAQ 当不存在 | 要。D9，`quota.py` |
| 按腐烂窗花钱（lag+waste） | 无。usher 轴是「谁最擅长这题」 | 要。D7 |
| A 答完 B 看得见（圆桌 / 辩论 / 接力） | 无。OpenACP 明文单 agent；usher 离开后不再包一层 | 要。`orchestrator.py` |
| 聊天桥 Telegram | acpbot / OpenACP | 不要长大。D11 冻结 |

所以 `quota.py` + `score.py` + 父对话 / 编排，才是维护面。再写一个启动器是和 usher 重复。

## 推荐路径

1. **对照 usher，不要对照 acpbot。** 单次「选人然后进 TUI」用 `macbot --tui`（或直接装 usher）。默认 REPL 留下来，只因为还要跨家同一场对话。
2. 从 usher 偷 UX，不偷轴：落座行、`--why` 表、doctor 名册、`-p`、failover 文案、`--print --json` envelope、stdin 重放、`--timeout`。不偷任务类型口碑表、动画 banner、`--agent` 强制路由。
3. acpbot 只在「还要不要养 Telegram」时有用。D11 已经冻结传输层，不再是默认替代品。
4. OmniRoute 仍然是反代 completion，D1 否掉。
