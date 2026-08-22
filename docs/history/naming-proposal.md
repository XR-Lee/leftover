# 公开名：换掉 MacBot

**2026-08-22 · 已拍板 leftover，落地见 D15。**

## 结论

公开名、CLI、PyPI、未来仓库用同一个词：**leftover**。

```
leftover "migrate sessions onto JWT"
leftover · Cursor
```

一句话：Spend leftover coding-CLI quota before the window resets.

`macbot` 留一个次版本 alias。agora 只作内部目录，直到搬仓。不要把包名改成 `macbot`。

觉得 leftover 太像剩菜 → 用 **idlespend**（更字面，github.com/idlespend 空）。不要用 agora / whichcli / surplus。

## 为什么 MacBot 不行

名字在卖两件产品已经否掉的东西。

| 词 | 听起来 | 事实 |
|---|---|---|
| Mac | 苹果个人脚本 | Python + ACP，不是 Mac-only。开源第一问是 "Linux?" |
| Bot | Telegram 聊天机器人 | D7/D11：父对话 + 官方 CLI 当 subagent。Telegram 冻结 |

GitHub 上已经有多个 `macbot`（ROS 栈等）。PyPI `macbot-cli` 已被占：*macOS automation CLI for AI agents*。连「Mac 上的 agent CLI」这条也有人用了。

它也不编码真正的楔子：按浪费窗花钱，不是「又一个 coding agent」。

## 最短路径是 agora。别走

包名 `agora-chat`、模块 `agora`、未来 git 根已经写在 D11。改口不改目录是最短的。

三个原因否掉：

1. **Agora.io** 占了 Agora / Agora Chat。npm `agora-chat` 是他们的 IM SDK。一搜就是 RTC，不是额度路由。
2. 隐喻是广场。对应已冻结的 Telegram 圆桌，不是薄 router。
3. D6 还在说 agora 大部分该扔。把要扔掉的层的名字当品牌，开源之后改第二次。

## 这赛道已经有人，楔子必须不一样

| 项目 | 它卖什么 | 和这里的差别 |
|---|---|---|
| [usher](https://github.com/theodorebeaupre-prog/usher) | One command, the right agent. 任务类型 × 口碑权重，exec 进官方 TUI 然后离开 | **最近的同类，不是只拿来挡名字。** 架构对照见 [prior-art.md](prior-art.md) / D13。启动器，不是父对话。额度只靠本地目击，不读官方 remaining |
| [subrouter](https://pypi.org/project/subrouter/) | 订阅账号 + API key 分流 | D1 禁的那类 |
| [anycli](https://pypi.org/project/anycli/) | 驱动本地 agent CLI 的 async 接口 | 库，不选窗 |
| [macbot-cli](https://pypi.org/project/macbot-cli/) | macOS automation for agents | 名字撞车 |

本产品开源时不该再讲「帮你选对 agent」。usher 已经把那句说满了。这里能赢的只有一句：

**同一场本地对话里，把活派给正在腐烂的官方额度窗；拒绝则换人，从不静默。**

所以 `whichcli` 这类 unix 玩笑也否掉——它把预期拉到 usher 的轴上（谁最擅长这题），而不是 lag+waste。

## 公式对命名的约束

打分是 `0.5 * lag + 1.0 * waste`。根 README 已经写了：月底才重置、看起来很空的 Ultra，打不过一小时后清零的 5h 窗。

**surplus / headroom 当产品名会广告权重更低的那一项。** 否掉。

要编码的是 waste：剩的、闲的、快过期的。

## leftover

| | |
|---|---|
| 意思 | 剩的额度。对准 waste，不是「最好的模型」 |
| Show HN | Leftover: spend leftover Codex/Grok/Cursor quota, in one local conversation |
| CLI | `leftover "…"` / `leftover doctor` / `leftover quota` |
| 登记 | PyPI `leftover` **空**（2026-08-22）。github.com/leftover 有用户，npm `leftover` 有包。Python CLI 只卡 PyPI |
| 声音 | 不像 Mac 脚本，不像 bot，不像 Agora.io。带一点自嘲，README 第二行必须立刻变干 |

风险：食物浪费 SEO；有人当玩笑。用产品句压住：Not a bot. Not a second TUI. Official CLIs only.

## 备选（只留两个）

**idlespend** — idle window + spend。字面、干、不好看。PyPI / npm / github.com/idlespend 都空。leftover 觉得轻就用它。

**nowaste** — 口号。PyPI / npm 空，github.com/nowaste 有组织。比 leftover 更运动，不如 leftover 好记。

## 不要的

| 名 | 为什么 |
|---|---|
| MacBot / macbot | 上文 |
| agora / agora-chat | 上文 |
| whichcli / usher | 错误的轴 |
| surplus / headroom | 错误的权重 |
| spend | PyPI 空，词太泛，搜不到 |
| sash | Unix 里已有 Stand-alone shell |
| leftover 当目录名但 CLI 仍叫 macbot | 现在这种双名就是病 |

## 拍板后怎么迁（现在不动）

一次对齐，不要第三套：

| | 现在 | leftover |
|---|---|---|
| 说话 / announce | MacBot · Cursor | leftover · Cursor |
| CLI | `macbot` | `leftover`，`macbot` alias 一个次版本 |
| PyPI | agora-chat | leftover |
| 配置 | `~/.config/macbot/` | `~/.config/leftover/`（读旧路径一轮） |
| skill | `~/.grok/skills/macbot` | leftover，ABI 仍是 `--pick --json`，`run` 换成 `leftover --print` |
| 未来 git 根 | D11 写的 `agora/` | 公开仓名 leftover；目录跟着改，别再让 agora 当对外词 |
| `Projects/MacBot/` | 混目录 | 仍然不要在这一层 `git init` |

D11 里「包名 agora-chat → macbot」作废。应是 agora-chat → leftover。

## 要你拍的只有一件

leftover，还是 idlespend。

定了再改入口、包名、skill、announce。在这之前不要 `git init`、不要发 PyPI。
