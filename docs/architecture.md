# 架构说明

## 组件与职责

| 组件 | 镜像 | 职责 |
| --- | --- | --- |
| NapCat | `mlikiowa/napcat-docker:v4.18.41` | 运行 QQNT 客户端，负责 QQ 登录、消息收发；以 OneBot v11 协议上报事件、执行动作 |
| AstrBot | `soulter/astrbot:v4.29.1` | 消息流水线（唤醒、白名单、限流、插件、LLM 调用）、会话上下文、人格、WebUI、模型管理 |
| init | 与 AstrBot 同镜像 | 一次性任务：运行 `scripts/render_config.py`，生成/同步两端的 OneBot 连接配置 |
| qq_guard 插件 | 挂载进 AstrBot | 群白名单（不影响私聊）、按用户限流、自消息防护、`/guard` 状态指令 |
| 模型服务 | 外部 | 任意 OpenAI 兼容 Chat Completions 接口 |

本项目没有重新实现 AstrBot、NapCat 或 OneBot 协议，只做了：部署编排、连接配置的单一事实来源（`.env`）、
安全默认值、一个小插件，以及测试与文档。

## 消息链路

```
 ┌──────┐   QQ 协议    ┌────────────────────────┐  OneBot v11 反向 WebSocket   ┌──────────────────────────────┐  HTTPS   ┌──────────┐
 │  QQ  │ ◀──────────▶ │ NapCat (容器 napcat)    │ ───────────────────────────▶ │ AstrBot (容器 astrbot)        │ ───────▶ │ LLM API  │
 └──────┘              │  WebSocket 客户端       │  ws://astrbot:6199/ws        │  aiocqhttp 平台：0.0.0.0:6199 │ ◀─────── │ (OpenAI  │
                       │  Authorization: Bearer │ ◀─────────────────────────── │  流水线 → 插件 → LLM          │          │  兼容)   │
                       └────────────────────────┘   动作调用 send_*_msg 等       └──────────────────────────────┘          └──────────┘
                              ▲ 127.0.0.1:6099 (WebUI)                                   ▲ 127.0.0.1:6185 (WebUI)
```

1. NapCat 收到 QQ 消息，转换为 OneBot v11 事件（`messagePostFormat: array`），通过已建立的反向 WebSocket 推送给 AstrBot。
2. AstrBot 的 `aiocqhttp` 适配器校验访问令牌（握手时 `Authorization: Bearer <token>`；缺失返回 401，错误返回 403），
   把事件转换为内部消息并进入流水线：
   `WakingCheck → Whitelist → SessionStatus → RateLimit → ContentSafety → PreProcess → Process(插件/LLM) → ResultDecorate → Respond`
3. 需要回复时，AstrBot 通过同一条 WebSocket 发起 `send_private_msg` / `send_group_msg` 等动作，NapCat 执行并返回结果。

## 网络设计

- `astrbot` 与 `napcat` 在同一个自定义 bridge 网络 `botnet` 中，NapCat 通过**服务名** `astrbot` 访问 AstrBot，
  不依赖容器 IP；容器重建、IP 变化都不影响连接。
- 6199（OneBot 反向 WS）只 `expose` 给容器网络，不映射到宿主机。
- 两个 WebUI 只映射到宿主机 `127.0.0.1`（可通过 `WEBUI_BIND_HOST` 修改，不推荐）。
- `init` 使用 `network_mode: none`，不需要任何网络。

## 启动顺序与配置同步

```
init (render_config.py) ──完成(exit 0)──▶ astrbot ──started──▶ napcat
```

`init` 每次 `docker compose up` 时运行：

- `data/astrbot/cmd_config.json` 不存在 → 写入种子配置（OneBot 平台、安全默认值、可选模型），其余字段由 AstrBot 首次启动补全；
- 已存在 → 只同步受管理字段：平台 `napcat` 的 `ws_reverse_host/port/token`、`ignore_bot_self_message=true`；
- NapCat：`onebot11.json`（模板）与 `onebot11_<QQ号>.json`（登录后实际使用）中名为 `astrbot` 的 WebSocket 客户端的
  `url/token/reportSelfMessage/messagePostFormat`。

令牌为空、过短或仍是示例值时 `init` 以非零退出码结束，`astrbot` 与 `napcat` 不会启动。

## 可靠性设计

| 需求 | 实现 |
| --- | --- |
| 断线自动重连 | NapCat WebSocket 客户端 `reconnectInterval: 5000`（毫秒）；AstrBot 作为服务端接受重连（集成测试覆盖） |
| 容器崩溃恢复 | `restart: unless-stopped`；AstrBot 有 healthcheck |
| 防自回复循环 | NapCat `reportSelfMessage: false`；AstrBot `ignore_bot_self_message: true`；qq_guard 再次比对发送者与机器人 QQ |
| 群聊只在明确触发时回复 | AstrBot 唤醒规则（@、回复机器人、`/` 前缀）；`ignore_at_all: true` |
| 白名单与权限 | AstrBot 会话白名单 + 管理员（`admins_id`）+ 指令权限；qq_guard 群白名单（只限群聊） |
| 频率限制 | AstrBot 会话级限流（默认 20 条/60 秒，丢弃）；qq_guard 按用户限流（默认 6 次/60 秒，仅统计已唤醒消息） |
| 日志与诊断 | AstrBot 日志文件 `data/astrbot/logs/`；Docker 日志轮转；`scripts/doctor.*` |
| 持久化 | `data/astrbot`（配置、SQLite 数据库、插件配置）、`data/napcat/config`、`data/napcat/qq`（登录态） |
| 密钥管理 | 所有密钥只在 `.env`；模型 Key 以 `$LLM_API_KEY` 引用的形式存在于配置中 |

## qq_guard 插件

位置：`plugins/astrbot_plugin_qq_guard/`，以绑定挂载（bind mount）方式进入 `/AstrBot/data/plugins/`。

- 用 `CustomFilter` 只匹配**已唤醒**的消息，普通群聊消息不会因为插件而进入后续流水线，也不消耗配额；
- 以 `priority=100` 先于其他插件执行；拦截时调用 `event.should_call_llm(True)` 与 `event.stop_event()`，
  阻止默认 LLM 请求和后续处理；
- 纯逻辑在 `guard.py`（无 AstrBot 依赖，单元测试覆盖），AstrBot 接线在 `main.py`；
- 配置项由 `_conf_schema.json` 定义，可在 WebUI「插件」中修改。
