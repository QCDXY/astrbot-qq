# AstrBot + NapCat QQ AI 聊天机器人

基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot)（消息处理、AI 对话、上下文、插件、权限）与
[NapCat](https://github.com/NapNeko/NapCat-Docker)（QQ 登录与消息收发）的 QQ 机器人部署项目，
两者通过 **OneBot v11 反向 WebSocket** 连接，用 Docker Compose 一键部署，面向个人与小规模 QQ 群。

```
QQ ⇄ NapCat ──OneBot v11 反向 WS──▶ AstrBot ⇄ 大语言模型 API（OpenAI 兼容）
        ws://astrbot:6199/ws（容器网络内，带访问令牌）
```

主要特性：

- 私聊直接对话；群聊**只在被 @、回复机器人或使用 `/` 指令时**响应（`@全体成员` 不触发）。
- 每个会话（每个私聊 / 每个群）独立上下文，重启后保留。
- 模型、人格、消息规则全部在 AstrBot WebUI 中配置；支持任何 OpenAI 兼容接口，可随时切换供应商。
- 两端访问令牌由 `.env` 统一管理并自动同步；6199 端口不暴露到宿主机；管理界面默认只绑定 `127.0.0.1`。
- 断线自动重连、防自回复循环、会话级 + 用户级频率限制、群白名单（不影响私聊）、日志落盘、`doctor` 诊断脚本。

> ⚠️ **风险提示**：NapCat 通过非官方方式接入 QQ（基于 QQNT 客户端），存在账号被风控、限制登录甚至封禁的风险，
> 也可能因 QQ 版本更新而暂时失效。请使用小号，不要用于批量发送、营销或骚扰消息。本项目不修改 QQ 客户端安全机制，
> 不包含任何绕过风控的功能。

---

## 目录

1. [前置要求](#1-前置要求)
2. [快速开始](#2-快速开始)
3. [日常使用](#3-日常使用)
4. [启动、停止、更新、备份](#4-启动停止更新备份)
5. [项目结构](#5-项目结构)
6. [测试](#6-测试)
7. [待人工联调项目](#7-待人工联调项目)
8. [更多文档](#8-更多文档)

---

## 1. 前置要求

| 系统 | 需要 |
| --- | --- |
| Windows 10/11 | [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)（使用 WSL 2 后端），PowerShell |
| Linux | Docker Engine 24+ 与 Compose 插件（`docker compose version` 能输出版本） |

另外需要：一个用于机器人的 QQ 号（建议小号）、一部登录了该 QQ 的手机（扫码用）、一个 OpenAI 兼容模型服务的 API Key。

镜像体积：AstrBot 约 1.5 GB，NapCat 约 1 GB。国内网络拉取缓慢时，请在 Docker Desktop → Settings → Docker Engine 中配置镜像加速或代理。

## 2. 快速开始

### 2.1 获取项目并生成 `.env`

把本项目放到任意目录（路径中最好不要有空格），然后在项目根目录执行：

**Windows PowerShell**

```powershell
cd D:\astrbot-qq                   # 换成你的项目路径
.\scripts\init-env.ps1
```

> 如提示“禁止运行脚本”，先执行：`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

**Linux**

```bash
cd ~/astrbot-qq
chmod +x scripts/*.sh
./scripts/init-env.sh
```

脚本会从 `.env.example` 生成 `.env`，并为 `ONEBOT_ACCESS_TOKEN`（两端共享的 OneBot 访问令牌）与 `NAPCAT_WEBUI_TOKEN`
填入随机值；Linux 版还会把 `NAPCAT_UID/GID` 设为当前用户。已有 `.env` 时不会覆盖。

### 2.2 编辑 `.env`

用记事本 / VS Code / `nano` 打开 `.env`，至少填写模型三项：

```ini
LLM_API_KEY=你的 API Key
LLM_BASE_URL=https://api.deepseek.com/v1     # 你的服务商的 OpenAI 兼容地址（通常以 /v1 结尾）
LLM_MODEL=deepseek-chat                        # 模型 ID
```

可选：

```ini
ADMIN_QQ=你的QQ号            # 管理员，可用管理指令，且不受群白名单/限流约束；多个用逗号分隔
QQ_ACCOUNT=机器人QQ号         # 填写后 NapCat 会尝试快速登录，减少扫码次数
ASTRBOT_DASHBOARD_INITIAL_PASSWORD=   # AstrBot WebUI 初始密码；留空则随机生成
```

说明：

- `LLM_BASE_URL` / `LLM_MODEL` 只在 AstrBot **首次初始化**时写入模型配置，之后请在 WebUI 中修改。
- API Key **不会**写入任何配置文件：配置中保存的是 `$LLM_API_KEY` 引用，AstrBot 运行时从环境变量读取。
  修改 `.env` 中的 Key 后执行 `docker compose up -d` 即可生效。
- 不填模型也能启动，稍后在 WebUI 中添加（见 [2.6](#26-配置模型可选)）。

### 2.3 启动容器

**Windows PowerShell**

```powershell
.\scripts\start.ps1
```

**Linux**

```bash
./scripts/start.sh
```

也可以直接执行 `docker compose up -d`。启动顺序为：

1. `init`（一次性）：根据 `.env` 生成/同步两端的连接配置，令牌不合法时会报错并阻止启动；
2. `astrbot`：监听容器网络内的 `0.0.0.0:6199`；
3. `napcat`：启动 QQ，并作为客户端连接 `ws://astrbot:6199/ws`。

首次启动需要拉取镜像，请耐心等待。用 `docker compose ps` 查看状态，`init` 显示 `Exited (0)` 是正常的。

### 2.4 NapCat 扫码登录 QQ（需要人工操作）

1. 浏览器打开 **http://127.0.0.1:6099/webui**，输入 `.env` 中的 `NAPCAT_WEBUI_TOKEN` 登录。
   （若 `NAPCAT_WEBUI_TOKEN` 留空，令牌会随机生成并打印在 `docker compose logs napcat` 中。）
2. 在“快速登录 / 扫码登录”页面用**手机 QQ 扫码**，并在手机上确认登录。
   也可以从日志中看二维码：`docker compose logs -f napcat`。
3. 登录成功后，NapCat 会按配置自动连接 AstrBot。登录态保存在 `data/napcat/qq/`，重启容器通常无需重新扫码
   （QQ 侧仍可能要求重新验证，届时重复本步骤）。

> 本项目不会、也不应代替你完成 QQ 扫码或任何账号验证。

### 2.5 确认 WebSocket 已连接

**Windows PowerShell**

```powershell
docker compose logs astrbot | Select-String "适配器已连接"
.\scripts\doctor.ps1
```

**Linux**

```bash
docker compose logs astrbot | grep "适配器已连接"
./scripts/doctor.sh
```

看到 `aiocqhttp(OneBot v11) 适配器已连接。` 即表示 NapCat 已通过 `ws://astrbot:6199/ws` 接入 AstrBot。
`doctor` 会逐项检查 Docker、`.env`、Compose 配置、两端令牌/地址是否一致、端口暴露、容器网络与日志。
连不上时请看 [docs/troubleshooting.md](docs/troubleshooting.md)。

### 2.6 登录 AstrBot WebUI 并确认模型

1. 打开 **http://127.0.0.1:6185**，用户名 `astrbot`，初始密码：
   - 如果在 `.env` 中设置了 `ASTRBOT_DASHBOARD_INITIAL_PASSWORD`，就是它；
   - 否则查看日志：`docker compose logs astrbot | Select-String -Pattern "password"`（Linux 用 `grep -i password`）。
2. 首次登录后按提示**修改密码**。
3. 打开左侧「**模型提供商**」：如果在 `.env` 中填了模型，这里已有名为 `default_llm` 的提供商，可点击测试连通性。

#### 配置模型（可选）

没有在 `.env` 中填写模型，或想切换供应商时：「模型提供商」→ 新增 → 选择 **OpenAI Compatible**（或对应厂商模板）→
填写 API Base URL 与 API Key → 添加模型 ID → 保存。API Key 一栏可以填 `$LLM_API_KEY`，从而继续由 `.env` 管理密钥。
多个提供商之间可在 WebUI 或用 `/provider` 指令切换。

#### 人格与消息规则

- 「**人格设定**」：设置系统提示词（机器人性格、说话风格）。
- 「**配置文件**」→ 平台/消息相关设置：唤醒前缀（默认 `/`）、回复时是否 @ 发送者、会话白名单、会话级频率限制、
  `unique_session`（群内每人独立上下文）等。
- 「**插件**」→ `astrbot_plugin_qq_guard` → 配置：群白名单、按用户限流（见 [3.3](#33-群白名单与频率限制)）。

### 2.7 首次对话

1. 用你自己的 QQ **私聊**机器人：发送 `你好`，应收到模型回复。
2. 把机器人拉进群，在群里 **@机器人** 说话；不 @ 的普通群聊消息机器人不会回复。
3. 发送 `/help` 查看指令，`/sid` 查看会话 ID，`/guard` 查看群号与限流状态。

没有回复时：先看 `docker compose logs -f astrbot` 中是否有收到消息的记录（形如 `[napcat(aiocqhttp)] 昵称/QQ号: 内容`），
再看是否有模型调用错误（API Key / Base URL / 模型 ID 错误会在日志中显示）。

## 3. 日常使用

### 3.1 何时回复

| 场景 | 是否回复 |
| --- | --- |
| 私聊任意消息 | 回复 |
| 群聊 @机器人 / 回复机器人的消息 | 回复 |
| 群聊以 `/` 开头的指令（如 `/help`） | 执行指令 |
| 群聊普通消息、`@全体成员` | 默认不回复；开启**自然接话**的群里，机器人会旁听并在合适时机主动接话（见 3.4） |
| 机器人自己发出的消息 | 忽略（防循环） |

群聊上下文默认开启：机器人在群里回复时（无论是被 @ 还是自然接话），都会带上自它上次发言以来的群聊内容（最多 50 条），
所以能接上大家正在聊的话题。

### 3.2 常用指令（AstrBot 内置 + 本项目插件）

| 指令 | 说明 | 权限 |
| --- | --- | --- |
| `/help` | 帮助信息 | 所有人 |
| `/sid` | 查看会话 ID（UMO）与 QQ 号，用于白名单 / 管理员设置 | 所有人 |
| `/reset` | 清空当前会话上下文 | 私聊所有人；群内仅机器人管理员（开启 `unique_session` 后每人可重置自己的上下文） |
| `/new` | 新建对话 | 同上 |
| `/stop` | 停止当前正在生成的回复 | 所有人 |
| `/stats` | 当前对话的 token 用量 | 所有人 |
| `/provider` | 查看 / 切换模型提供商 | 机器人管理员 |
| `/guard` | 本项目插件：显示群号、群白名单状态、剩余配额 | 所有人 |

机器人管理员在 `.env` 的 `ADMIN_QQ` 中设置（仅首次初始化写入），之后在 WebUI「配置文件」中修改管理员 ID。

### 3.3 群白名单与频率限制

两层机制，均可在 WebUI 中修改，修改插件配置后会自动重载插件：

1. **AstrBot 内置**（「配置文件」中）：
   - 会话白名单：填写 `/sid` 显示的 UMO 或群号后，只有这些会话可用（私聊也受限制）；为空则不限制。
   - 会话级频率限制：默认每个会话 60 秒最多 20 条，超出丢弃。
2. **qq_guard 插件**（「插件」→ `astrbot_plugin_qq_guard` → 配置）：
   - `group_allowlist`：群白名单，只限制群聊，**不影响私聊**；为空表示所有群可用。在群里发 `/guard` 可看到群号。
   - `user_rate_limit_count` / `user_rate_limit_window`：按发送者限流，默认 60 秒 6 次，0 为不限；
     只统计已唤醒的消息（@、私聊、指令），超限时每个窗口只提示一次。
   - 管理员不受以上两项限制。

### 3.4 群聊自然接话（无需 @）

在 WebUI「插件」→ `astrbot_plugin_qq_guard` → 配置中，把群号加入 `natural_chat_groups` 即可开启（群白名单非空时，
该群还必须在白名单里）。开启后机器人像普通群友一样旁听：

- 不会每句都回：只有群聊**停顿**几秒、自上次判断以来累积了**几条新消息**（或有人提到机器人的称呼）、
  且**冷却时间**已过时，才用一次很短的模型调用判断“现在适不适合接话”，判断为适合才真正回复；
- 真正的回复走正常对话流程，使用你在「人格设定」中的人格与会话历史，本功能不会修改人格提示词；
- 被 @ 时照常立即回复；主动发言有每小时上限，避免刷屏和模型费用失控。

| 配置项 | 默认 | 调整建议 |
| --- | --- | --- |
| `natural_chat_groups` | 空（关闭） | 开启的群号 |
| `natural_chat_name_triggers` | 空 | 机器人的称呼，如它的群昵称；第一个作为机器人的名字 |
| `natural_chat_min_new_messages` | 3 | 嫌太吵就调大，嫌太安静就调小 |
| `natural_chat_quiet_seconds` | 4 | 群聊停顿多少秒后再判断 |
| `natural_chat_cooldown_seconds` | 60 | 机器人发言后多久内不主动接话 |
| `natural_chat_max_replies_per_hour` | 20 | 每群每小时主动发言上限 |

每次“判断”都会调用一次模型（输出只有一个词，费用很低），频率大约是“每累积 N 条消息最多一次”。

### 3.5 联网搜索

使用 AstrBot 内置的网页搜索，由模型在需要时自动调用。开启方式见 [配置指南 · 联网搜索](docs/configuration.md#6-联网搜索)：
模型能力勾选 tool_use，「配置文件」中开启网页搜索并选择提供商（使用 DeepSeek 模型时可选 DeepSeek，复用同一个 Key）。
本项目插件会屏蔽开启工具调用后不安全的内置工具（向任意会话发消息、创建定时任务）。

### 3.6 日志

- 实时日志：`docker compose logs -f astrbot napcat`
- AstrBot 日志文件：`data/astrbot/logs/astrbot.log`（20 MB 轮转）；WebUI「数据与日志」中也可查看。
- 容器标准输出日志由 Docker 轮转（每个容器最多 3 × 10 MB）。

## 4. 启动、停止、更新、备份

| 操作 | Windows PowerShell | Linux |
| --- | --- | --- |
| 启动 | `.\scripts\start.ps1` 或 `docker compose up -d` | `./scripts/start.sh` 或 `docker compose up -d` |
| 停止 | `.\scripts\stop.ps1` 或 `docker compose down` | `./scripts/stop.sh` 或 `docker compose down` |
| 重启某个服务 | `docker compose restart astrbot` | 同左 |
| 查看状态 | `docker compose ps` | 同左 |
| 诊断 | `.\scripts\doctor.ps1` | `./scripts/doctor.sh` |
| 修改 `.env` 后生效 | `docker compose up -d` | 同左 |

**数据持久化**：所有运行数据都在 `data/` 下，`docker compose down` 不会删除它们：

| 路径 | 内容 |
| --- | --- |
| `data/astrbot/` | AstrBot 配置 `cmd_config.json`、数据库（对话历史、人格等）、插件配置、日志 |
| `data/napcat/config/` | NapCat 配置（OneBot 连接、WebUI） |
| `data/napcat/qq/` | QQ 登录态（敏感，切勿外传）。登录态还与容器的主机名和 MAC 绑定，`compose.yaml` 已将其固定，请勿删除 |

**备份**：停止容器后复制整个 `data/` 目录（和 `.env`）即可；恢复时放回原处再启动。

**更新镜像**：修改 `.env` 中的 `ASTRBOT_IMAGE` / `NAPCAT_IMAGE` 为新的版本标签，然后：

```bash
docker compose pull
docker compose up -d
```

升级前建议先备份 `data/`。本项目在 AstrBot `v4.29.1` 与 NapCat `v4.18.41` 上验证。

**更换 OneBot 令牌**：修改 `.env` 中的 `ONEBOT_ACCESS_TOKEN` 后执行 `docker compose up -d`，
`init` 会把新令牌同步到两端（包括 NapCat 登录后生成的 `onebot11_<QQ号>.json`），然后执行
`docker compose restart astrbot napcat` 让两端重新加载。请不要只在某一端的 WebUI 中单独修改令牌。

**远程服务器访问管理界面**：保持 `127.0.0.1` 绑定，用 SSH 隧道访问：

```bash
ssh -L 6185:127.0.0.1:6185 -L 6099:127.0.0.1:6099 user@your-server
```

然后在本机浏览器打开 `http://127.0.0.1:6185` 与 `http://127.0.0.1:6099/webui`。

## 5. 项目结构

```
.
├── compose.yaml                 # 部署：init / astrbot / napcat 三个服务
├── .env.example                 # 环境变量示例（复制为 .env）
├── plugins/
│   └── astrbot_plugin_qq_guard/ # AstrBot 插件：群白名单、按用户限流、自消息防护、/guard
├── scripts/
│   ├── render_config.py         # 生成/同步两端 OneBot 连接配置（init 服务运行）
│   ├── init-env.ps1 / .sh       # 生成 .env（随机令牌）
│   ├── start.ps1 / .sh          # 启动
│   ├── stop.ps1 / .sh           # 停止
│   └── doctor.ps1 / .sh         # 诊断
├── tests/                       # 自动化测试（静态、单元、集成）
├── docs/
│   ├── architecture.md          # 架构说明
│   ├── configuration.md         # 配置指南
│   ├── troubleshooting.md       # 故障排查
│   └── acceptance-report.md     # 验收报告
└── data/                        # 运行数据（自动创建，已被 .gitignore 排除）
```

## 6. 测试

测试不需要 Docker、QQ 账号或真实 API Key。需要 [uv](https://docs.astral.sh/uv/)（或 Python 3.12 + pip）：

```bash
uv sync                      # 安装开发依赖（包含与镜像同版本的 astrbot==4.29.1）
uv run pytest                # 全部测试（集成测试约 2–3 分钟）
uv run pytest -m "not integration"   # 只跑快速的静态/单元测试
```

- **静态测试**：Compose 官方 JSON Schema 校验、端口/网络/卷/重启策略/镜像版本、`.env.example`、`.gitignore`、
  密钥扫描、脚本语法（bash / PowerShell 5.1 与 7）、`init-env` 脚本行为、README 覆盖度。
  设置 `DOCKER_COMPOSE_BIN` 或安装 Docker 后会额外运行 `docker compose config` 校验。
- **单元测试**：`render_config.py`（令牌同步、不覆盖用户修改、NapCat 按账号配置文件同步）、qq_guard 逻辑。
- **集成测试**：在临时目录启动**真实的 AstrBot 4.29.1 进程**，使用本项目生成的配置和插件；用模拟 NapCat
  （OneBot v11 反向 WS 客户端）和模拟 OpenAI 兼容服务验证：私聊回复、群聊 @ 才回复、会话上下文隔离、指令、
  群白名单、按用户限流、防自回复、错误/缺失令牌被拒绝（403/401）、断线重连、WebUI 可访问、重启后配置与对话历史保留、
  API Key 错误时不会伪造成功回复。

最近一次测试结果见 [docs/acceptance-report.md](docs/acceptance-report.md)。

## 7. 真实环境联调

以下项目需要真实 QQ 账号、手机扫码和真实模型 API Key，已于 2026-10-23 在 Windows + Docker Desktop 上人工联调通过
（详见 [验收报告](docs/acceptance-report.md#5-真实环境联调结果2026-10-23)）：

- [x] `docker compose up -d`，三个服务正常启动（`init` 退出码 0，`astrbot` healthy）
- [x] NapCat WebUI 扫码登录成功，`data/napcat/qq/` 中生成登录态
- [x] AstrBot 日志出现 `aiocqhttp(OneBot v11) 适配器已连接。`，`doctor` 全部通过
- [x] 真实模型调用成功（私聊收到 DeepSeek 回复）
- [x] 私聊机器人收到模型回复；群里 @机器人 收到回复，不 @ 不回复
- [x] `docker compose down` + `up -d` 后无需重新扫码即可继续对话，且上下文仍在

在你自己的环境部署后，建议按同样的清单走一遍。尚未在真实环境验证（已由自动化测试覆盖）：白名单外群聊被忽略、按用户限流、不同用户上下文隔离。

## 8. 更多文档

- [架构说明](docs/architecture.md)
- [配置指南](docs/configuration.md)（所有 `.env` 变量、自动生成的配置、插件配置、切换模型供应商）
- [故障排查](docs/troubleshooting.md)
- [验收报告](docs/acceptance-report.md)
- 官方文档：[AstrBot](https://docs.astrbot.app/) · [AstrBot 接入 OneBot v11](https://docs.astrbot.app/platform/aiocqhttp.html) · [NapCat](https://napneko.github.io/)
