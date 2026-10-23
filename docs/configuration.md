# 配置指南

## 1. `.env` 变量

`.env` 由 `scripts/init-env.*` 从 `.env.example` 生成，已被 `.gitignore` 排除。修改后执行 `docker compose up -d` 生效。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ASTRBOT_IMAGE` | `soulter/astrbot:v4.29.1` | AstrBot 镜像（也用于 init 服务） |
| `NAPCAT_IMAGE` | `mlikiowa/napcat-docker:v4.18.41` | NapCat 镜像 |
| `TZ` | `Asia/Shanghai` | 时区 |
| `ONEBOT_ACCESS_TOKEN` | 随机生成 | **必填**。OneBot 反向 WS 访问令牌，两端共享，≥16 位，不能含空白和双引号 |
| `ASTRBOT_WEBUI_PORT` | `6185` | AstrBot WebUI 宿主机端口 |
| `NAPCAT_WEBUI_PORT` | `6099` | NapCat WebUI 宿主机端口 |
| `WEBUI_BIND_HOST` | `127.0.0.1` | 两个 WebUI 绑定的宿主机地址。改为 `0.0.0.0` 会暴露到局域网/公网，不推荐 |
| `NAPCAT_WEBUI_TOKEN` | 随机生成 | NapCat WebUI 登录令牌，仅首次（`webui.json` 不存在时）生效；留空则 NapCat 随机生成 |
| `ASTRBOT_DASHBOARD_INITIAL_PASSWORD` | 空 | AstrBot WebUI 初始密码，仅首次生效；至少 8 位且含大写、小写字母和数字（init 会提前校验）；留空随机生成并打印在日志中 |
| `QQ_ACCOUNT` | 空 | 机器人 QQ 号，用于 NapCat 快速登录 |
| `ADMIN_QQ` | 空 | 管理员 QQ 号，逗号分隔，仅首次初始化写入 `admins_id` |
| `LLM_API_KEY` | 空 | 模型 API Key，只注入 astrbot 容器的环境变量 |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容接口地址，仅首次初始化写入 |
| `LLM_MODEL` | `gpt-4o-mini` | 模型 ID，仅首次初始化写入 |
| `NAPCAT_UID` / `NAPCAT_GID` | `1000` | NapCat 容器内运行用户；Linux 上应与 `data/` 目录所有者一致 |
| `NAPCAT_MAC_ADDRESS` | `02:42:ac:11:00:02` | NapCat 容器固定 MAC。QQ 登录态与设备特征（MAC、主机名）绑定，固定后 `down/up`、升级镜像无需重新扫码；同机多个 NapCat 实例需各用不同值。主机名在 `compose.yaml` 中固定为 `napcat-qq` |

“仅首次”的含义：对应配置文件已经存在时，这些变量不再覆盖，以 WebUI 中的修改为准。

## 2. 自动生成的配置

### AstrBot：`data/astrbot/cmd_config.json`

首次部署时 `init` 写入的种子（其余字段由 AstrBot 用官方默认值补全）：

```json
{
  "config_version": 5,
  "platform": [{"id": "napcat", "type": "aiocqhttp", "enable": true,
                "ws_reverse_host": "0.0.0.0", "ws_reverse_port": 6199, "ws_reverse_token": "<ONEBOT_ACCESS_TOKEN>"}],
  "platform_settings": {
    "rate_limit": {"time": 60, "count": 20, "strategy": "discard"},
    "enable_id_white_list": true, "id_whitelist": [],
    "ignore_bot_self_message": true, "ignore_at_all": true,
    "friend_message_needs_wake_prefix": false, "unique_session": false
  },
  "admins_id": ["<ADMIN_QQ>"],
  "wake_prefix": ["/"],
  "log_file_enable": true, "log_file_path": "logs/astrbot.log", "log_file_max_mb": 20,
  "provider_ltm_settings": {"group_icl_enable": true, "group_message_max_cnt": 50},
  "provider_sources": [{"id": "llm", "type": "openai_chat_completion", "provider_type": "chat_completion",
                        "key": ["$LLM_API_KEY"], "api_base": "<LLM_BASE_URL>", "...": "..."}],
  "provider": [{"id": "default_llm", "provider_source_id": "llm", "model": "<LLM_MODEL>", "modalities": ["text", "tool_use"], "enable": true}],
  "provider_settings": {"web_search": false, "proactive_capability": {"add_cron_tools": false}}
}
```

之后每次启动只同步：平台 `napcat` 的监听地址/端口/令牌，以及 `ignore_bot_self_message: true`。
平台的“启用/禁用”状态尊重 WebUI 中的选择。

> `modalities` 默认只有 `text`，兼容性最好。如果你的模型支持图片或工具调用，可在 WebUI「模型提供商」中勾选。

### NapCat：`data/napcat/config/onebot11*.json`

与 NapCat-Docker 官方 `templates/astrbot.json` 一致，仅填入令牌并把重连间隔缩短为 5 秒：

```json
{"network": {"websocketClients": [{"enable": true, "name": "astrbot", "url": "ws://astrbot:6199/ws",
  "reportSelfMessage": false, "messagePostFormat": "array", "token": "<ONEBOT_ACCESS_TOKEN>",
  "heartInterval": 30000, "reconnectInterval": 5000}], "httpServers": [], "websocketServers": [], "...": []}}
```

NapCat 登录后会把 `onebot11.json` 复制为 `onebot11_<QQ号>.json` 并以后者为准，`init` 会同步两者。
在 NapCat WebUI 中新增其他网络配置不受影响；名为 `astrbot` 的客户端的 `url/token/reportSelfMessage/messagePostFormat`
会被恢复为 `.env` 对应的值。

检查两端是否一致（只读）：

```bash
docker compose run --rm --no-deps init --astrbot-data /AstrBot/data --napcat-config /napcat/config --check
```

## 3. 模型配置与切换供应商

推荐通过 AstrBot WebUI「模型提供商」管理。常见 OpenAI 兼容地址示例（以各服务商官方文档为准）：

| 服务 | Base URL 示例 |
| --- | --- |
| OpenAI | `https://api.openai.com/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| 其他兼容服务 / 自建网关 / 本地 Ollama 等 | 服务提供的 `/v1` 地址 |

切换方式：

1. **改 Key**：修改 `.env` 的 `LLM_API_KEY`，`docker compose up -d`。
2. **改地址/模型**：WebUI「模型提供商」中编辑 `default_llm`。
3. **新增供应商**：WebUI 中新增提供商，Key 可以填 `$某个环境变量名`，并在 `compose.yaml` 的 astrbot `environment` 中
   增加该变量（值写在 `.env`）；然后用 WebUI 或 `/provider` 切换。也可以使用 AstrBot 支持的其他原生供应商模板。

## 4. 人格与消息规则

- 人格：WebUI「人格设定」新建人格（系统提示词），再在「配置文件」中设为默认人格。
- 唤醒前缀：「配置文件」`wake_prefix`（默认 `/`）。
- 群内每人独立上下文：「配置文件」`unique_session` 设为开启。
- 回复时 @ 发送者 / 引用原消息：`reply_with_mention` / `reply_with_quote`。
- 会话白名单：`id_whitelist` 中填入 `/sid` 显示的 UMO 或群号。

## 5. qq_guard 插件配置

WebUI「插件」→ `astrbot_plugin_qq_guard` → 配置（保存在 `data/astrbot/config/astrbot_plugin_qq_guard_config.json`）：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `group_allowlist` | `[]` | 群号列表。为空 = 所有群可用；非空时只有列出的群能唤醒机器人。私聊和管理员不受影响 |
| `user_rate_limit_count` | `6` | 每个用户在窗口内最多唤醒次数，0 = 不限 |
| `user_rate_limit_window` | `60` | 窗口长度（秒） |
| `notify_on_limit` | `true` | 超限时是否提示（每个窗口最多一次） |
| `natural_chat_groups` | `[]` | 开启“群聊自然接话”的群号；为空 = 关闭。群白名单非空时还需在白名单内 |
| `natural_chat_name_triggers` | `[]` | 机器人的称呼；群友提到时尽快判断是否接话。第一个作为判断时机器人的名字 |
| `natural_chat_min_new_messages` | `3` | 至少累积多少条新消息才判断一次 |
| `natural_chat_quiet_seconds` | `4` | 群聊停顿多少秒后再判断（停顿期间有新消息则由最新那条负责判断） |
| `natural_chat_cooldown_seconds` | `60` | 机器人发言（含被 @ 的回复）后多久内不主动接话 |
| `natural_chat_max_replies_per_hour` | `20` | 每群每小时主动发言上限，0 = 不主动发言 |
| `natural_chat_context_size` | `20` | 判断时参考的最近消息条数 |
| `blocked_llm_tools` | `["send_message_to_user", "future_task"]` | 不提供给模型的内置工具（见第 6 节）；清空表示不屏蔽 |

群聊上下文由 AstrBot 内置的 `provider_ltm_settings.group_icl_enable` 提供（种子配置默认开启，保留最近 50 条），
在 WebUI「配置文件」中可以关闭或调整条数。AstrBot 内置的“主动回复”（`active_reply`，按固定概率随机回复）保持关闭，
由 qq_guard 的自然接话代替。

## 6. 联网搜索

使用 AstrBot 内置的网页搜索（无需额外插件）。模型通过工具调用决定何时搜索，回答时可以引用搜索结果。

1. 「模型提供商」中模型的能力（modalities）需包含 **tool_use**（工具调用），否则 AstrBot 会在请求前移除所有工具。
   本项目的种子配置已默认包含；旧部署需在 WebUI 中勾选。
2. 「配置文件」→ 网页搜索：开启 `web_search`，选择搜索提供商并填写其 API Key。可选提供商：
   Tavily、BoCha（博查）、Brave、Firecrawl、百度 AI 搜索、Exa、AnySearch、DeepSeek、SerpApi。
   使用 DeepSeek 模型时可选 **DeepSeek**，直接复用同一个 DeepSeek API Key（调用 `https://api.deepseek.com/v1/tools/search`）。
3. 保存后生效。开启后模型会获得 `web_search_<提供商>`（及抓取网页全文的工具，若该提供商支持）。

注意：

- 搜索 Key 由 AstrBot 以明文保存在 `data/astrbot/cmd_config.json`（AstrBot 暂不支持像模型 Key 那样用 `$环境变量` 引用）。
  `data/` 已被 `.gitignore` 排除，请勿外传该目录。
- 开启 tool_use 后 AstrBot 还会自动提供 `send_message_to_user`（可向任意会话发消息、可发送本地文件）和
  `future_task`（创建定时任务）。这两个工具对群聊不安全，qq_guard 插件的 `blocked_llm_tools` 默认将其屏蔽；
  种子配置同时关闭了 `proactive_capability.add_cron_tools`。
- 是否搜索由模型决定，人格设定会影响这一点：如果人格要求“像普通网友一样、不确定就说不知道”，模型可能较少主动搜索；
  在提问中明确说“搜一下”会更可靠。
- 模型知道“今天几号”依赖「配置文件」中的 `datetime_system_prompt`（默认开启）；关闭后模型不知道当前日期，
  搜索“最新/今天”类问题的效果也会变差。

## 7. 升级

1. 备份 `data/`；
2. 阅读 AstrBot / NapCat 的发布说明；
3. 修改 `.env` 中的镜像标签，`docker compose pull && docker compose up -d`；
4. 运行 `scripts/doctor.*` 并做一次私聊测试；
5. 同步修改 `pyproject.toml` 中的 `astrbot==` 版本并运行 `uv run pytest`，确认插件与配置在新版本上仍然通过。
