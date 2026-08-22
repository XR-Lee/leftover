# 决策记录

新决策往上加。状态：**生效** / **待定** / **已推翻**。

维护面（核心 feature、模块图、git 根）在 `docs/`。这里只记「为什么」。

---

## D15 — 公开名 leftover 已落地，仓是 XR-Lee/leftover

**2026-08-22 · 生效**

D12 拍板 leftover。CLI / PyPI / announce / skill / 仓同一词。`macbot` 留一个次版本 alias。Python 包从 `agora` / `agora-chat` 改成 `leftover`。配置先读 `~/.config/leftover/leftover.toml`，仍读一轮 macbot / agora 旧路径。状态先写 `leftover-state.json`，仍读 `macbot-state.json`。

git 根是本目录，远程 `git@github.com:XR-Lee/leftover.git`。不要在混着 AutoPaperReview 的 `Projects/MacBot/` 上 init。

---

## D14 — 偷 usher 的终端表面，不偷轴，不做 banner

**2026-08-22 · 生效**

D13 把 `--why` / `--tui` / `continuation_guard` 对齐了功能。人看到的还是 agora 时代的 `MacBot · Codex`、啰嗦 doctor、`--print` 的 `macbot: trying`。usher 已经把 launcher 的可见交互做对了，再发明一套更差。

**采用（同形，不同轴）：**

- 落座行 `→ Codex  (coding · lag+waste · override with @name)`。`--print` 加 `· headless`。skill `announce` 仍是 `MacBot · Codex`。
- `--why` 表：`task:` + remaining 条 + `← launching`。列是 lag/waste/total，没有 strength。
- `macbot doctor`：名册 + remaining 条（缓存的 reported/observed）+ 安装提示 + config/state/ledger 路径。不现场打 usage 接口。
- `-p` 是 `--print`。piped stdin 重放到每一次尝试。撞墙换人时 stderr：`→ Codex hit its cap — failing over to Grok (with continuation notice)`。
- `--print --json` 是一次 run 的 envelope（agent/kind/exit_code/output/attempts）。`--json` 不带 `-p`、以及 `--pick --json`，仍是 skill pick dump。
- `--timeout 90s|2m` 只给 `-p`。超时退出 124。REPL 提示对齐 README 的 `you>`。

**不采用：** 动画 banner；`--agent` 当强制路由；任务类型 × 口碑；把 `--tui` 改成默认。

---

## D13 — 最近的同类是 usher；偷 UX，不偷轴，不重写启动器

**2026-08-22 · 生效**

[usher](https://github.com/theodorebeaupre-prog/usher)（MIT，Go，v0.3）已经是「一条命令选官方 coding CLI，exec 进自家 TUI，撞墙换人」。D6 对照的是 acpbot/OpenACP/OmniRoute，看错类。命名提案（D12）发现它之后只拿来挡 `whichcli`，没写进 `prior-art.md`。

**不采用：** 任务类型 × 口碑权重；「厂商不暴露剩余额度」所以只靠本地目击；默认把人丢进官方 TUI 然后离开。那三件和 D7/D9/D11 相反。

**采用（对齐 usher 已经做对的交互）：**

- `--why`：把 lag+waste 打成一张表再停，和 usher `--why` 同形、不同轴。
- `continuation_guard`：同一请求里换人时，后手当前手可能改过工作区。默认开，toml 可关。
- `--tui` 继续是 usher 那条路，不是默认 REPL。默认仍是父对话。

不要 fork usher，不要用它的 adapter 表给 Grok/Cursor 打分。

详见 [prior-art.md](prior-art.md)。

---

## D12 — 公开名从 MacBot 换掉

**2026-08-22 · 生效**（落地见 D15）

MacBot 当开源名不行：Mac 锁死平台，Bot 像 Telegram，GitHub/PyPI 已有同名近邻（含 `macbot-cli`）。agora 是最短改口，但撞 Agora.io，隐喻还停在已冻结的圆桌。

提案：[naming-proposal.md](naming-proposal.md)。推荐公开名 **leftover**（CLI/PyPI/仓同一词），备选 **idlespend**。不要 whichcli（usher 的轴），不要 surplus（广告了权重更低的 lag）。

拍板前不改代码、不 init 仓、不发 PyPI。D11 里「agora-chat → macbot」作废，应迁到新公开名。`macbot` 只留 alias。

---

## D11 — 维护面钉死：薄 CLI router，不是第二个 terminal 产品

**2026-08-22 · 生效**

MacBot 的核心 feature 已经齐了，以后进独立 git 仓时只带这些，不把仓库做成舰队或套壳 TUI。

**要维护的：** `macbot` REPL / `--print` / `--pick --json` / `--tui`；`/plan` `/cu` `@name`；lag+waste；拒绝分类 + 熔断；ACP→exec；`/rt` `/all` `/debate` `/relay`；`install-skills`；四家官方 usage 探测。

**不维护、不准长大的：** Telegram（`transports/telegram.py` + `render.py`）；`agora console`；fork/套 Grok 或 Codex pager；反代 completion。D2 的「前端是 Telegram」只覆盖 `agora bot` 这条遗留路径，主路径以 D7 为准。

**未来 git 根是 `agora/`，不是 `Projects/MacBot/`。** 同目录下的 AutoPaperReview / paper_review 不进这个仓。`notes/` 跟着走，变成 `docs/history/`。在混目录上 `git init` 会把无关树一并锁死。

Skill ABI：`--pick --json` 的 `run` 永远是 `macbot --print …`，skills 禁止 `exec spawn`。`--agent` 是调用者身份，不是点名。

详见 `agora/docs/core-features.md` 和 `agora/docs/maintenance.md`。

---

## D10 — GPT/Codex 额度优先读 Sub2API admin，不把 admin key 写进仓库

**2026-08-22 · 生效**

Codex session 日志经常把 `used_percent` 写成 null，`/quota` 对 gpt 是空的。本机已经有一份 Sub2API，账号 `calmabacus`（openai oauth）上有 Codex 5h/7d。MacBot 用 admin key 只读 `GET /api/v1/admin/accounts` + `/usage?source=active`，**不**把 completion 打进反代。

密钥只放 `~/.config/macbot/macbot.toml`（0600）或 `SUB2API_ADMIN_API_KEY`。读不到再回退 `~/.codex/sessions`。

---

## D9 — 额度探测打各家官方 usage 接口，用官方 CLI 已经存好的登录，不 spawn 套壳、不打 grok.com gRPC-web

**2026-08-21 · 生效**

D1 禁的是反代 completion。撞墙前的剩余额度只存在厂商服务器上，官方 CLI 自己已经在打这些只读接口。MacBot 用同一份本地登录去读，窗才会在选人之前动。

| 家 | 数字从哪来 | 不做什么 |
|---|---|---|
| Claude | `GET /api/oauth/usage`（Keychain / `.credentials.json`） | 不造 `claude usage` 子命令；不把 OAuth 拿去跑 Messages |
| Grok | `GET cli-chat-proxy.grok.com/v1/billing?format=credits` | 不为了 /quota spawn `grok agent stdio`；不打 grok.com gRPC-web |
| Cursor | `GetCurrentPeriodUsage` + IDE `state.vscdb` token | 不把 `cursor-agent about` 的计划名当成剩余额度 |
| Codex | Sub2API admin `/accounts/:id/usage`（配了才打）；否则 `~/.codex/sessions` | 不把 GPT 的 completion 走反代 |

Grok 的 SuperGrok 窗是**周**的（实测 `creditUsagePercent` + `USAGE_PERIOD_TYPE_WEEKLY`）。Cursor 的 included 池是**月**的。Claude 仍是 5h + 7d（外加按模型的 weekly_scoped）。读失败就诚实降到 estimated / 拒绝后再 observed。自动降级还是骨干。

---

## D8 — 不 fork、不套壳 Grok/Codex TUI。MacBot 只选人，TUI 永远是官方的

**2026-08-21 · 生效**

Grok Build 和 Codex 的开源 terminal **不是通用壳**。它们是那一家的 harness：登录、工具循环、子代理、额度、diff/rewind/worktree 全绑在自家协议上。套上去当 MacBot 前端，会立刻撞上三件已经钉死的事：

1. **额度只在官方 CLI 进程里走。** Grok TUI 里切 custom model（OpenAI-compatible completion）等于 D1 已经否掉的反代：丢掉 Codex computer use / Grok subagent / Claude 工具循环，窗也不一定动。
2. **两家都不收外部 PR。** [xai-org/grok-build](https://github.com/xai-org/grok-build) 是 monorepo 定期 dump（`SOURCE_REV`），CONTRIBUTING 写明不接受贡献；[openai/codex](https://github.com/openai/codex) 同样不收 PR，TUI 还在往 app-server 拆。fork = 每周跟一个不给你合并通道的内部仓库 rebase。
3. **TUI 功能不能跨家搬。** Codex 的本机 computer use、Grok 的 `x.ai/*` ACP 扩展、Cursor Ultra 第一方模型，都不是「换个 model id」。一个套壳进程里做不到四家 harness 同时是真的。

**维护模型（从短到长，只走前两档）：**

| 档 | 做法 | 谁维护 TUI | 额度 / harness |
|---|---|---|---|
| A | skill + `macbot --pick`，该谁干谁干，该走就 `exec` 进官方 TUI | 厂商 | 对 |
| B | MacBot 自己的 ACP 对话（跨家接力、Telegram、撞墙换人） | 我们只维护薄 client | 对 |
| C | Grok/Codex **plugin**（skill/hooks/`/macbot`），不改 TUI 源码 | 厂商；我们跟 skill 格式 | 对，但人还在这一家里 |
| D | fork `grok-build` / `codex` 换后端 | 我们 | 错，或永远在合并 |

A 已经有了（`install-skills` + `--tui`）。B 是现在的默认 `you>` REPL。C 只在「人已经坐在 Grok 里、想要一个 slash 去挑人」时才值得做。D 不做。

非要二选一 fork 的话 Grok 比 Codex 轻（crate 边界清楚、有 plugin/hooks/custom model），但轻不等于可维护——上游照样不收 PR，pager 绑 `x.ai/*`。所以这题的正确答案不是「套 Grok 还是套 Codex」，是 **不要套**。

想要 Grok 那种 fullscreen / 鼠标 / subagent UI：`macbot --tui`，让挑中的那家自己画。想要跨家同一场对话：留 ACP client，不要去偷 pager。

---

## D7 — MacBot 是薄 CLI router，复用 agora 的额度层

**2026-08-21 · 生效**

主路径是 `macbot` 作为**父对话**：`/plan` `/cu` `@name` + 编码池滞后/浪费打分，把官方 CLI 当 **subagent**（ACP harness）拉起来干活，结果回到这场对话。不是换「用户在跟谁聊」。额度探测、拒绝分类、熔断继续用 `quota.py` / `router.py`。Telegram 圆桌不是这条路径。

编码池：`codex`、`grok`、`cursor-agent --model grok-4.6`。Claude 只接 `/plan` 和编码全灭。本机 computer use 交给 Codex CLI。

浪费项只认 reported/observed，estimated 的 turn budget 不许伪装成 5 小时紧急窗。

---

## D6 — agora 的大部分应该被现成项目替代

**2026-08-21 · 待定 · 2026-08-22 更正对照对象**

当时结论：acpbot/OpenACP 覆盖传输层，OmniRoute 覆盖路由降级，只剩 `orchestrator.py`。**对照对象错了。** 最近的同类是 usher（launcher + 目击额度 + failover），不是聊天桥。OmniRoute 仍是 D1 否掉的反代。

更正后还值得自研的是 usher 不做的三件：官方 remaining、lag+waste、父对话/圆桌。启动器不要再写。Telegram 去留仍待定，但 D11 已冻结长大。

**教训不变**：先写后查。第二次是发现了 usher 却只当品牌墙。

详见 `prior-art.md`、D13。

---

## D5 — 路由和降级是两件事，分开实现

**2026-08-21 · 生效**

- **降级**永远有效：它拒绝 → 分类 → 关禁闭 → 同一请求内换下一个。
- **路由**只在有额度信号时才有意义。四家现在都能在登录后给出撞墙前数字；读失败时仍然没有预警。

所以降级是骨干，路由是锦上添花。反过来设计（先做漂亮的打分器，降级当兜底）会在 Claude 撞墙那天原形毕露——因为对 Claude 你根本拿不到撞墙前的预警。

策略可切换：`headroom` / `order` / `cheapest` / `sticky`。

---

## D4 — 拒绝消息必须在 Router 层分类，不能在 runner 层

**2026-08-21 · 生效**

Claude Code 把 `You've hit your weekly limit` 当**正常回答正文**返回，不是 error。第一版真的把它打进了聊天窗口。

现在 300 字符以内的短回复也过一遍分类器——足够抓住限额消息，又不会把一篇讨论限流的长文误判成拒绝。

放在 Router 而不是 runner 里，是因为四家的拒绝形态各不相同（有的走 error，有的走正文，有的走 stop_reason），但**处理方式完全一样**。

---

## D3 — ACP 优先，exec 兜底，自动降级

**2026-08-21 · 生效**

两种接法的差别只有一个：**进程活多久**。

- **ACP**：一个常驻进程横跨多轮，session_id 不变，流式输出，工具调用可见。
- **exec**：一轮一个进程，用完即死，没有跨轮记忆，输出一次性返回。

ACP 明显更好，但各家的 ACP 子命令拼法会漂移。所以 `transport = "auto"`：ACP 起不来就静默降级到 headless。路由、降级、记账对两者完全一样。

---

## D2 — 前端是 Telegram bot，跑在自己 Mac 上

**2026-08-21 · 生效**

云端 Grok bot 干不了重活：额度有限、没有仓库访问、没有 agent loop。本地跑就能 `/cd` 到真实仓库，让 agent 用自己的工具改文件。Telegram 当前端是为了手机上也能触发，体验和已有的 Grok bot 一致。

Grok 在配置里标成 `light` tier，只接短平快的发言，额度留着。重活路由到 Claude Max 和 ChatGPT 的额度上。

---

## D1 — 走官方 CLI，不走反代订阅端点

**2026-08-21 · 生效**

原始需求里说过"无所谓，要最爽的体验"，但反代在这个场景里恰恰是**不爽**的那个：

CLIProxyAPI 那类东西给的是**裸模型补全**，会丢掉 Claude Code 的工具循环、Codex 的沙箱执行、Grok Build 的 subagent 和 worktree。既然要"讨论 + 编程都要"，编程那一半的价值几乎全在 agent loop 里，反代之后就没了。

顺带的好处是不用担心封号。

**唯一的例外场景**：如果哪天只想要纯对话的多模型群聊、完全不碰代码，反代 + LibreChat 确实更省事。到那时再单独评估。

---

# 待决问题

- [ ] 默认交互要不要从 MacBot `you>` REPL 改成 `exec` 进官方 TUI（`--tui` 当默认）？usher 已经选了 launcher。这边默认留 REPL 是因为父对话是楔子，不是因为没见过那条路。跨家接力仍走 ACP chat
- [ ] 要不要做一个 Grok/Codex **plugin** 包装现有 skill（`/macbot`），而不是继续只靠 `install-skills` 拷 SKILL.md
- [ ] acpbot 能不能让两个 agent 读同一份上下文？如果能，连编排层都省了
- [ ] OmniRoute 的订阅层到底怎么拿的 token——spawn CLI 还是反代？决定它能不能用
- [x] `codex-acp@1.6.2`、`grok agent stdio`、`cursor-agent --model grok-4.6 acp` 三条命令实测
- [ ] Claude 和 Cursor 的 5 小时 / 周额度上限到底是多少轮？budget 只在 usage 接口失败时还用得上
- [ ] 要不要 Discord？OpenACP 三个平台都支持，acpbot 只有 Telegram。**在 D11 下默认是否：不加。**
- [x] 独立 git 仓：`agora/` 为根，公开名 leftover，远程 XR-Lee/leftover（D15）
