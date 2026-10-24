# QQ 官方机器人接入指南（dsh-qqbot）

> 本指南对应**官方方案**：用腾讯 connect 团队维护的 [`dsh-qqbot`](https://github.com/tencent-connect/dsh-qqbot)
> 插件，把 DeepSeek 接入 **QQ 官方机器人 API v2**。合规、零封号风险，无需第三方逆向协议。
>
> 与「小号 + NapCat」第三方方案的区别见文末「方案对比」。

---

## 一、方案简介

- `dsh-qqbot`（`@tencent-connect/dsh-qqbot`）是腾讯 connect 团队基于
  [deepseek-harness (dsh)](https://github.com/deepseek-ai/deepseek-harness) 开发的插件。
- 走 **QQ 官方 API**（依赖 `qqbot-nodejs` SDK），机器人是**官方机器人账号**（非你的小号）。
- 支持**私聊（C2C）+ 群聊**，带流式输出、会话隔离、图片理解、文件发送、斜杠命令等能力。
- 首次启动**自动扫码引导**完成「开放平台注册 → 创建机器人 → 绑定」，无需手动抄 AppID/AppSecret。

数据流：

```
QQ 用户 → QQ WebSocket 网关 → dsh-qqbot 插件 → dsh agent loop → DeepSeek
                                        ↓
                          (回复 → QQ sendMarkdown)
```

---

## 二、前置条件

| 项目 | 要求 |
| --- | --- |
| Node.js | 18+（本机已装 22.22.2） |
| QQ | 一个已完成实名认证的 QQ 号（扫码注册开放平台用） |
| DeepSeek | 一个可用的 DeepSeek API Key |

> ✅ **好消息：你已经在用 DSH 了**。检测到 `~/.dsh` 下已有 `web` / `desktop` profile，
> 且 `.credentials.yaml` 里已有 `deepseek-account-platform/default` 账号 token（以及 GLM/Kimi 的 key），
> 说明 DeepSeek 侧已就绪，**只需装 qqbot 插件 + 扫码绑定 QQ 机器人**即可。

---

## 三、安装与启动

### 1. 安装 dsh 框架并添加 qqbot 插件

> ⚠️ **请在你自己电脑的普通终端（PowerShell / CMD / Git Bash）里执行**，不要在带文件删除保护的
> 沙箱/代理终端里跑（会因 npm 清理缓存被拦截而失败）。命令如下：

```bash
npx -y @deepseek-ai/dsh plugin --profile qqbot add @tencent-connect/dsh-qqbot
```

### 2. 启动（首次自动扫码绑定）

```bash
npx @deepseek-ai/dsh --profile qqbot
```

首次启动时插件检测到凭据未配置，会**自动进入扫码引导**：

- **桌面版 / Web 模式**：自动打开应用内「QQ Bot 绑定」页（右侧边栏），页面内渲染二维码；
- **CLI 模式**：终端直接输出二维码；
- 用手机 QQ 扫码 → 同意授权 → 凭据自动保存，后续启动无需再扫。

> 若终端二维码渲染错位，可设置 `QQBOT_BIND_OPEN_BROWSER=1` 强制用系统浏览器打开扫码页；建议升级到 `0.4.0` 以上版本。

### 3. 配置 DeepSeek

`dsh-qqbot` 默认 `provider: deepseek-official`、`model: deepseek-chat`。DeepSeek 的 API Key 在 **dsh 宿主层**配置：

- 通过 dsh 的 Web UI（默认 `http://127.0.0.1:3080`）在模型设置里填入 DeepSeek API Key；
- 或以 dsh 的 `settings.yaml` / 环境变量方式配置（字段以 dsh 文档为准）。

---

## 四、验证「收消息 → 处理 → 回复」

1. 启动后，打开手机/电脑 QQ，消息列表会出现刚创建的机器人账号；
2. **私聊**机器人发一句「你好」，机器人应回复（Markdown 格式）；
3. **群聊**：把机器人拉进「你是群主/管理员」的群，群里 **@机器人** 发消息触发回复；
4. 输入 `/bot-ping` 看网络延迟、`/bot-status` 看会话状态、`/bot-help` 看全部指令。

> ⚠️ 若机器人回复「该机器人去火星了」，说明凭据未打通，检查扫码绑定是否完成、AppID/AppSecret 是否正确。

---

## 五、个人主体的能力边界（重要）

QQ 官方机器人对**个人认证主体**的限制（2026-10 官方口径）：

| 能力 | 个人主体 |
| --- | --- |
| 创建机器人 | ✅ 可建 **5 个**，扫码即注册、点击即创建，**无需审核** |
| 私聊（C2C） | ✅ 需在管理端「开发设置」添加**开发体验用户**（上限 20 人） |
| 群聊 | ⚠️ **暂不支持公开使用**；仅能添加「自己是群主/管理员」的群，进群上限 500 |
| 群聊触发 | ⚠️ 群里需 **@机器人** 才会触发（官方「只推被 @」模式） |
| 公开上架 | ❌ 仅企业主体可公开 |

> 若需公开给陌生人/任意群使用，需升级**企业主体**（个体户也可，需营业执照 + 对公账号）。

---

## 六、常用配置项（`cordis.yml` / 环境变量）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `appId` / `appSecret` | 必填 | QQ Bot 凭据（或 `QQBOT_APPID` / `QQBOT_SECRET`） |
| `provider` | `deepseek-official` | LLM 提供商 |
| `model` | `deepseek-chat` | 模型 |
| `requireMention` | `true` | 群聊是否需 @ 触发 |
| `streaming` | `true` | 流式输出（群聊不启用） |
| `sessionIdleTimeout` | `1800000` | 会话闲置超时 30 分钟 |
| `access.c2cMode` | `open` | 私聊访问：open / allowlist / disabled |
| `access.groupMode` | `open` | 群聊访问：open / allowlist / disabled |
| `vision.enabled` | `false` | 图片视觉理解（需另配 vision provider） |
| `media.enabled` | `true` | 富媒体理解 |

---

## 七、方案对比（官方 vs 第三方小号）

| 维度 | 官方 dsh-qqbot（本指南） | 第三方小号 NapCat（本项目 Python 代码） |
| --- | --- | --- |
| 合规/封号 | ✅ 官方，零风险 | ⚠️ 有风控/冻结风险 |
| 机器人身份 | 独立机器人账号 | 你的小号即机器人 |
| 私聊 | ✅（个人限 20 体验用户） | ✅ 无限制 |
| 群聊 | ⚠️ 仅自己管理的群 + 需 @ | ✅ 任意群、监听全部消息 |
| **主动发消息** | ⚠️ 需好友关系 + 频控 + 对方可关「允许主动发送」 | ✅ 真人号，主动私聊/进群/加人几乎无限制 |
| 功能 | 流式/图片/文件/斜杠命令，开箱即用 | 需自行开发业务逻辑 |
| 维护 | 腾讯官方团队 | 社区 |

**结论**：要「合规省心、@一下就用」→ 选官方；要「**真人化主动**（随时主动私聊/进群/加人）、任意群监听」→ 选第三方小号（接受风控风险）。

> 📌 **主动消息硬性限制（官方口径）**：官方机器人主动私聊需先建立好友关系（对方先加机器人好友/先聊过），
> 频控为个人认证 10 条/秒、每好友每天最多 1000 条，且用户在客户端可关闭「允许主动发送」导致发送失败。
> 小号方案无此类限制。
