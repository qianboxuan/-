# DeepSeek 接入 QQ

让 QQ 机器人监听群聊与私聊消息，转发给业务逻辑（默认可选 Echo 回显 / DeepSeek 大模型）处理，并把结果回复到对应会话。聊天接入以**独立、可插拔的模块**实现，不侵入业务逻辑。

---

## 1. 接入方案与理由

### 选型：OneBot 11 标准协议 + WebSocket

| 方案 | 说明 | 结论 |
| --- | --- | --- |
| **QQ 官方机器人开放平台** | 官方提供 WebSocket/HTTP 事件订阅，合规性最好 | 主要面向 **QQ 频道**；普通群的群聊机器人需**企业认证**，个人难以申请，且对普通群私聊/群聊的通用监听支持有限，无法满足「个人监听群聊+私聊」的诉求 |
| **第三方协议实现（OneBot 11）** | NapCatQQ / LuckyLilliaBot / SnowLuma 等，基于 NTQQ 实现 | ✅ **选用**。支持普通 QQ 群聊 + 私聊监听，个人小号可用，生态成熟 |

**选择 OneBot 11 的理由：**

1. **事实标准**：`OneBot 11` 是 QQ 机器人接入最通用的协议规范，各协议端均兼容，换协议端无需改代码。
2. **覆盖需求**：能监听「群聊 + 私聊」，正是本项目的核心诉求；官方开放平台对此支持有限。
3. **语言无关、实现简单**：通过 WebSocket 上报事件、接收 API 调用（JSON），Python 用 `websockets` 即可实现，无需逆向协议。
4. **机制标准化**：连接生命周期、心跳、事件类型（`message`/`notice`/`meta_event`）都有明确定义，断线重连、异常处理有章可循。

### 协议端选型（2026-10 全网调研）

「小号接入」的本质：用一个 QQ 小号登录协议端，协议端把小号收到的群聊/私聊消息转成 OneBot 11 事件推给本项目。当前（2026-10）各协议端状态：

| 协议端 | GitHub | 状态（2026-10） | 特点 / 结论 |
| --- | --- | --- | --- |
| **NapCatQQ** | [NapNeko/NapCatQQ](https://github.com/NapNeko/NapCatQQ) | 🟢 活跃（持续更新） | **首选**。无头、内存低（50~100MB）、内置 WebUI（默认 `http://127.0.0.1:6099`）、多小号隔离、接替 go-cqhttp 的最主流方案 |
| **LuckyLilliaBot**（原 LLOneBot） | [LLOneBot/LuckyLilliaBot](https://github.com/LLOneBot/LuckyLilliaBot) | 🟢 活跃（v8.x） | **备选**。原 LLOneBot 更名而来，支持 OneBot 11 / Satori / Milky 多协议，已有独立 CLI/Desktop 模式 |
| **SnowLuma** | [SnowLuma/SnowLuma](https://github.com/SnowLuma/SnowLuma) | 🟢 新兴黑马（增长快） | NapCat 官方 README 推荐其为新一代 GUI 替代品；WebUI `http://localhost:5099`，多账号隔离。注意：**非 OSI 完全开源，商用需授权**，个人自用可考虑 |
| **Lagrange.Core** | [LagrangeDev/Lagrange.Core](https://github.com/LagrangeDev/Lagrange.Core) | 🔴 已归档（2025-10） | 开发者停止维护，**不推荐** |
| **go-cqhttp** | [Mrs4s/go-cqhttp](https://github.com/Mrs4s/go-cqhttp) | 🔴 停止维护 | 已被 NapCatQQ 取代，不推荐 |

> ⚠️ **合规与风控提示**：第三方协议实现基于 NTQQ 客户端注入/模拟，存在账号被风控、封号的风险，且不属官方授权渠道。个人自用 / 学习 / 内部测试场景可用；**生产环境或对稳定性、合规性要求高的场景，建议改用官方开放平台或企业机器人**，届时只需按本项目 `ChatAdapter` 接口新增一个适配器即可（见第 3 节「可插拔设计」）。

### 连接模式

本项目适配器同时支持两种模式（由 `chat.onebot11.mode` 控制）：

- **`reverse`（默认，推荐）**：本项目作为 WebSocket **服务端**，协议端以「反向 WS」主动连入上报事件。配置最简单、跨协议端通用，断线重连由协议端负责。
- **`forward`**：本项目作为 WebSocket **客户端**主动连接协议端「正向 WS」地址，断线后由本项目**按指数退避自动重连**。

---

## 2. 项目结构

```
DeepSeek接入QQ/
├── main.py                  # 入口：装配配置/日志/处理器/适配器并启动
├── requirements.txt         # 依赖
├── config.example.yaml      # 配置模板（复制为 config.yaml 使用）
├── config.yaml              # 本地配置（已 gitignore）
├── .env.example             # 环境变量模板
├── app/
│   ├── config.py            # 配置加载（YAML + .env + 环境变量覆盖）
│   ├── logging_setup.py     # 统一日志（控制台 + 文件，UTF-8）
│   ├── chat/                # ★ 聊天接入模块（独立、可插拔）
│   │   ├── base.py          #   ChatAdapter 抽象接口
│   │   └── onebot11.py      #   OneBot 11 实现（连接/重连/事件/回复）
│   └── business/            # ★ 业务逻辑模块（与聊天接入解耦）
│       ├── handler.py       #   MessageHandler 抽象接口 + ChatContext
│       ├── echo.py          #   Echo 处理器（联调用）
│       └── deepseek.py      #   DeepSeek 处理器
└── scripts/
    ├── smoke_test.py        # 冒烟测试：reverse 模式「收→处理→回复」
    └── smoke_forward_test.py# 冒烟测试：forward 模式连接/重连/收发
```

### 可插拔设计

- **聊天接入**只依赖 `ChatAdapter` / `MessageHandler` 抽象接口，不感知业务细节。
- **业务逻辑**只实现 `MessageHandler.handle(ctx) -> str | None`，不感知底层是 QQ 还是其它渠道。
- 新增渠道：继承 `ChatAdapter` 并在 `main.py` 的 `ADAPTERS` 注册表登记即可。
- 新增业务：继承 `MessageHandler` 并在 `main.py` 的 `HANDLERS` 注册表登记即可。

---

## 3. 依赖安装

需要 Python 3.9+（本项目在 3.13 验证通过）。

```bash
# 1) 创建并激活虚拟环境
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

# 2) 安装依赖
pip install -r requirements.txt
```

> 若只用 `echo` 模式联调，可只装核心三件：`pip install websockets PyYAML python-dotenv`。
> `openai` 仅在 `business.handler = deepseek` 时被惰性导入，届时才需要。

---

## 4. 配置

配置优先级：**环境变量 > `.env` > `config.yaml` > 内置默认值**。账号、AppID、Token、监听对象等敏感/可变信息均通过配置管理，**不写死在代码中**。

```bash
cp config.example.yaml config.yaml   # 首次使用
cp .env.example .env                 # 可选，用环境变量方式管理敏感信息
```

关键配置项（`config.yaml`）：

```yaml
chat:
  adapter: onebot11
  onebot11:
    mode: reverse              # reverse=服务端(推荐) | forward=客户端
    host: 127.0.0.1            # reverse 模式监听地址
    port: 8080                 # reverse 模式监听端口
    path: /onebot/v11/ws       # 反向 WS 上报路径
    access_token: ""           # OneBot 鉴权 token，留空不鉴权
    reconnect:                 # forward 模式重连策略
      initial_delay: 1
      max_delay: 30
      backoff_factor: 2
      max_retries: -1
  listen:                      # 监听对象白名单（空=不过滤）
    groups: []                 # 仅监听这些群号
    private_users: []          # 仅监听这些私聊用户
    ignore_self: true          # 忽略机器人自己发的消息
business:
  handler: echo                # echo | deepseek
  deepseek:
    api_key: ""                # 建议改用环境变量 DEEPSEEK_API_KEY
    model: deepseek-chat
```

常用环境变量（完整见 `.env.example`）：`DEEPSEEK_API_KEY`、`QQBOT_ACCESS_TOKEN`、`QQBOT_MODE`、`QQBOT_HOST`、`QQBOT_PORT`、`QQBOT_HANDLER`、`QQBOT_LOG_LEVEL` 等。

---

## 5. 最小可运行示例

### 5.1 Echo 模式（无需任何外部 API）

```bash
python main.py
```

启动后日志出现 `反向 WS 服务已就绪，等待协议端连入……` 即成功。协议端连入后，对机器人发的任意消息都会回显 `[echo] 收到你的消息：xxx`。

### 5.2 DeepSeek 模式

```bash
# 方式一：环境变量
set DEEPSEEK_API_KEY=sk-xxxx        # Windows CMD
export DEEPSEEK_API_KEY=sk-xxxx     # bash/zsh
# 方式二：改 config.yaml 的 business.deepseek.api_key 与 handler: deepseek

python main.py
```

---

## 6. 本地联调：完整跑通「收消息 → 处理 → 回复」

### 方式 A：自动化冒烟测试（无需真实 QQ，最快）

已验证通过，直接运行：

```bash
python scripts/smoke_test.py          # reverse 模式：私聊/群聊收发 + 忽略自己
python scripts/smoke_forward_test.py  # forward 模式：连接 + 断线重连 + 收发
```

### 方式 B：接入真实 QQ 小号（端到端）

用**一个 QQ 小号**登录协议端，协议端把小号收到的群聊/私聊消息转成 OneBot 11 事件推给本项目。任选一种协议端（推荐 NapCatQQ）：

#### B1. NapCatQQ（推荐）

1. **装 NTQQ + NapCat**
   - 准备一个 QQ 小号；
   - 从 [NapCatQQ Release](https://github.com/NapNeko/NapCatQQ/releases) 下载最新版（Windows 有一键安装器），或参考官方文档 [start-install](https://napneko.github.io/guide/start-install) 用 Shell/Framework 方式安装；
   - 首次启动后访问 WebUI `http://127.0.0.1:6099`（首次进入需从启动日志里取 WebUI token）。
2. **扫码登录小号**：在 WebUI 里按提示用手机 QQ 扫二维码登录（⚠️ 同一小号不能同时登录官方 QQ 客户端和 NapCat）。
3. **配置反向 WebSocket**：WebUI → 「网络配置」→ 新增 **WebSocket 服务端（反向 WS）**，地址填：
   ```
   ws://127.0.0.1:8080/onebot/v11/ws
   ```
   若本项目配置了 `access_token`，在协议端同位置填入相同 token。
4. **启动本项目**（保持 `mode: reverse`、端口 8080）
   ```bash
   python main.py
   ```
   看到 `协议端已连入：...` 日志即连接成功。
5. **验证**：私聊小号发 `你好` → 收到 `[echo] 收到你的消息：你好`；群里 @小号发消息 → 收到回显；日志出现 `收到private/group消息…` 与 `回复…` 即完成闭环。

#### B2. LuckyLilliaBot（原 LLOneBot）

- 仓库 [LLOneBot/LuckyLilliaBot](https://github.com/LLOneBot/LuckyLilliaBot)，下载 Release（CLI / Desktop），同样扫码登录小号；
- 在 WebUI 里新增反向 WebSocket 地址 `ws://127.0.0.1:8080/onebot/v11/ws` 即可，其余步骤同上。

#### B3. SnowLuma（NapCat 官方推荐的新 GUI）

- 仓库 [SnowLuma/SnowLuma](https://github.com/SnowLuma/SnowLuma)，下载后运行启动器，浏览器打开 `http://localhost:5099`；
- 按向导挂载 QQ 进程并配置 OneBot 反向 WS 指向 `ws://127.0.0.1:8080/onebot/v11/ws`；
- 注意其为「非 OSI 完全开源」，个人自用没问题，商用需授权。

> 💡 联调通过后想切换成 DeepSeek：设置 `DEEPSEEK_API_KEY` 并把 `business.handler` 改为 `deepseek`（见第 5.2 节），重启 `python main.py` 即可让小号变身 AI 助手。

### 断线重连 / 异常验证

- **reverse 模式**：断开协议端（如退出 NapCat）再重开，本项目会打印连接断开/重连日志并继续工作。
- **forward 模式**：`mode: forward` + 指向协议端正向 WS 后，手动停掉协议端，观察日志出现 `N 秒后进行第 M 次重连`，恢复协议端后自动重连成功。

---

## 7. 关键日志说明

| 日志 | 含义 |
| --- | --- |
| `启动反向 WS 服务：ws://...` | 反向服务就绪 |
| `协议端已连入：...` | 协议端 WebSocket 连接建立 |
| `收到group/private消息 来自 昵称(QQ号) group=...：内容` | 收到一条消息并进入业务处理 |
| `回复group/private消息 -> ...：内容` | 业务结果已回发 |
| `协议端连接断开：...` / `N 秒后进行第 M 次重连` | 断线与重连 |
| `OneBot 生命周期事件：self_id=...` | 协议端登录上线 |

日志同时输出到控制台与 `logs/qqbot.log`（UTF-8，Windows 下无中文乱码）。

---

## 8. 常见问题

- **收不到消息**：确认协议端反向 WS 地址与端口/路径和本项目一致；确认机器人已登录；确认 `listen` 白名单未过滤掉目标会话。
- **中文乱码**：本项目日志已强制 UTF-8；若在旧终端看到乱码，设置 `chcp 65001` 或升级 Python 3.7+。
- **`openai` 缺失**：仅在 `deepseek` 模式下需要，`pip install -r requirements.txt` 即可。
- **风控/封号风险**：见第 1 节提示。小号使用建议：新号先养号（正常聊天一段时间再上机器人）、避免高频群发、同一小号不要既登录官方 QQ 又登录协议端、谨慎升级 QQ 客户端（协议端需适配新版）。生产环境请改用官方开放平台。
