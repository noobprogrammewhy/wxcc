# wxcc — 微信 ↔ Claude Code 桥

**在微信里直接使唤跑在你自己机器上的 Claude Code。**

微信收到消息 → Claude Code 处理（完整的 shell / 文件 / 编辑工具权限）→ 结果发回微信。支持**扫码绑定**、图片/文件双向收发、每个会话独立的持久上下文、微信内 `!` 命令（切模型 / 压缩上下文 / 查用量 / 切回历史会话……）。

微信接入层从 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)（MIT）的 `gateway/platforms/weixin.py` 抽取并解耦，走腾讯 **iLink Bot API**（个人微信机器人，官方接口，非 hook / 非协议逆向）。Agent 层用官方 **Claude Agent SDK** 驱动 Claude Code。

## 特性

- **扫码绑定** — 终端打印二维码，微信扫一下就连上
- **常驻会话** — 每个微信 chat 一个常驻 `ClaudeSDKClient`，免冷启动；进程重启后按 session id 自动 resume
- **媒体双向** — 微信发来的图片/文件/语音自动落地成本地路径给 Claude；Claude 回复里写 `MEDIA:/绝对路径` 即原生发文件回微信
- **微信工具** — Claude 可通过内置 `send_message` MCP 工具主动给任意微信会话推消息/文件
- **微信内命令** — 不开终端也能切模型、压缩上下文、查花费、切回历史对话（见下表）
- **访问控制** — first（首个私聊者锁定为 owner）/ allowlist / open 三种策略

## 架构

```
微信 ⇄ 腾讯 iLink Bot API ⇄ wxcc(long-poll)  ⇄  Claude Code (Agent SDK)
                                  │                    │
                          AES-CDN 媒体收发      每 chat 一个可 resume 的常驻会话
```

| 模块 | 职责 |
|------|------|
| `wxcc/ilink.py` | iLink 客户端：扫码登录、35s 长轮询、发文本/媒体、AES-128 CDN 加解密 |
| `wxcc/agent.py` | Claude Agent SDK 封装（常驻 `ChatSession`）+ `send_message` 微信工具 |
| `wxcc/bridge.py` | 轮询→Claude→回发；访问控制、按 chat 串行、`!` 命令、媒体桥接 |
| `wxcc/media.py` | `MEDIA:/path` 标签解析 + 入站媒体缓存 |
| `wxcc/formatting.py` | 微信文本分段（≤2000 字，按 Markdown 块切） |
| `wxcc/store.py` | 凭证 / 会话映射 / 会话历史 / 配置，落盘 `WXCC_HOME`（默认 `~/.wxcc`） |

## 安装

前置要求：

- Python ≥ 3.10
- [Claude Code](https://claude.com/claude-code) CLI（`claude` 在 PATH 上），并已登录（或配置 `ANTHROPIC_API_KEY`）
- 一个**独立的微信号**（见下方[关键说明](#关键说明)）

```bash
git clone https://github.com/noobprogrammewhy/wxcc.git && cd wxcc
python -m venv .venv
# Windows
.venv\Scripts\python -m pip install -r requirements.txt
# Linux / macOS
.venv/bin/python -m pip install -r requirements.txt
# 或 pip install -e .  （装上 `wxcc` 命令）
```

## 快速开始

下面 `wxcc` 表示 `python -m wxcc.cli`（或 `pip install -e .` 后的 `wxcc`）。

```bash
# 1) 扫码绑定微信（终端打印二维码，用微信扫；同时保存 qr.png 兜底）
wxcc login

# 2) 启动桥接守护进程（Windows 也可以直接双击 start.bat）
wxcc run

# 3) 绑定后用你自己的微信给机器人号发一条消息 —— 你会被锁定为 owner，
#    之后 Claude Code 就开始响应你了。

# 查看状态 / 改配置
wxcc status
wxcc config --set model=claude-opus-4-8
```

## 微信内命令

直接在微信里发这些 `!` 命令（由桥接拦截处理，不会转给 Claude）：

| 命令 | 作用 |
|------|------|
| `!help` | 显示命令帮助 |
| `!status` | 当前模型 / 会话 id / 是否热连接 / 目录 / 权限 / 排队数 |
| `!model` / `!model <名字>` | 查看 / 切换模型：`opus`/`sonnet`/`haiku`/`fable` 或完整 id；上下文保留 |
| `!cwd` / `!cwd <路径>` | 查看 / 切换工作目录（上下文保留） |
| `!perm` / `!perm <模式>` | 查看 / 切换权限模式：`default`/`acceptEdits`/`plan`/`dontAsk`/`bypassPermissions` |
| `!usage`（或 `!cost`） | 本会话花费 / 输入·输出·缓存 token / 轮次 / 耗时 |
| `!context`（或 `!ctx`） | 上下文窗口占用（走 SDK `get_context_usage()`，带进度条与分类） |
| `!compact [说明]` | 压缩上下文释放窗口（可加聚焦说明，如 `!compact 保留部署步骤`），显示前后占用 |
| `!reset`（或 `!new`） | 清空当前会话上下文重新开始（模型/目录/权限回到默认） |
| `!resume` / `!resume <编号>` | 列出 / 切回之前的历史对话（标题=该对话的第一句话） |
| `!stop`（或 `!interrupt`） | 打断正在进行的回复，并清空排队消息 |
| `!clear` | 只清空正在排队的待处理消息 |
| `!id`（或 `!whoami`） | 显示你的会话 id（配 `allow_from` 白名单用） |
| `!ping` | 探活 |

> `/model`、`/compact` 之类的 Claude Code 斜杠命令是交互式 CLI 的内建命令，在微信通道用不了——用上面的 `!` 命令替代。
>
> `!usage` 只能统计本会话的花费/token（来自每轮 `ResultMessage`，进程内累计）；订阅套餐的周额度无法通过 Agent SDK 获取，需在终端 `/usage` 查看。

## 配置

`~/.wxcc/config.json`（或 `wxcc config --set key=value`）：

| 键 | 默认 | 说明 |
|----|------|------|
| `dm_policy` | `first` | 谁能用：`first` 首个私聊者成为持久 owner / `allowlist` 白名单 / `open` 所有人（危险） |
| `allow_from` | `[]` | allowlist 模式下允许的 id 列表（微信里发 `!id` 查看） |
| `cwd` | `~/wxcc-workspace` | Claude Code 工作目录 |
| `permission_mode` | `bypassPermissions` | Claude Code 权限模式 |
| `model` | `null` | 默认模型（null = Claude Code 默认） |
| `system_prompt_extra` | `""` | 追加到系统提示的自定义内容 |
| `send_chunk_delay_seconds` | `1.5` | 长回复分段发送的间隔；调小回复更快，太小可能触发微信限频 |

## 访问控制（重要）

**这等于把你机器的 shell 暴露给微信对端**，务必限制谁能用：

- 默认 `dm_policy=first`：绑定后**你自己先发一条消息**把自己锁成 owner，其余人一律忽略。
- 更严格用 `allowlist`；`open` 别在任何真实环境用。
- 权限模式默认 `bypassPermissions`（自己的机器上无人值守跑活）。要保守可 `wxcc config --set permission_mode=acceptEdits`。

## 关键说明

- **需要一个独立微信号**：iLink 的 token 同一账号只能有一个轮询者，不能和其他 bot 部署共用同一个微信号。
- **发文件**：Claude 在回复里写一行 `MEDIA:/绝对路径`，该文件原生发到当前会话，标签从可见文本里去掉；跨会话推送用 `send_message` 工具。
- **会话上下文**：每个微信 chat 映射一个 Claude Code 会话（按 id resume），重启后延续；历史对话存 `~/.wxcc/session_history.json`，`!resume` 可切回。
- **群聊**：iLink 机器人身份通常进不了普通群，默认只处理私聊。
- **系统代理**：iLink 是腾讯国内接口，wxcc 刻意**不走**系统 `HTTP(S)_PROXY`（走代理每请求慢约 20 倍）；而 Claude 子进程正常继承系统代理环境变量——两条链路互不影响。
- **登录产物**：`wxcc login` 会在本地留下二维码 `qr.png` 与含账号 id 的日志，已在 `.gitignore` 里，别提交。

## 致谢

- 微信 iLink 协议实现改编自 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)（MIT License, © 2025 Nous Research）
- Agent 能力来自 [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview)

## License

[MIT](LICENSE)
