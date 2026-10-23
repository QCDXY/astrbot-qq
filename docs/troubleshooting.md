# 故障排查

先运行诊断脚本，它会逐项检查并给出提示：

```powershell
.\scripts\doctor.ps1      # Windows
```

```bash
./scripts/doctor.sh       # Linux
```

常用命令：

```bash
docker compose ps                         # 服务状态（init 为 Exited (0) 是正常的）
docker compose logs init                  # 配置同步输出 / 令牌错误提示
docker compose logs -f astrbot            # AstrBot 日志
docker compose logs -f napcat             # NapCat 日志（含二维码、连接状态）
```

---

## 启动阶段

### `init` 失败，astrbot / napcat 没有启动

`docker compose logs init` 会显示原因：

| 提示 | 处理 |
| --- | --- |
| `ONEBOT_ACCESS_TOKEN 为空` / `仍是示例值` / `太短` | 运行 `scripts/init-env.*`，或手动在 `.env` 中设置 ≥16 位随机字符串 |
| `NAPCAT_WEBUI_TOKEN 仍是示例值` | 替换为随机字符串或留空 |
| `... 不是合法 JSON` | 对应配置文件被手动改坏，修复或删除该文件（删除会重新生成默认值；`cmd_config.json` 删除后 WebUI 中的设置会丢失，请先备份） |

### 拉取镜像很慢或失败

在 Docker Desktop → Settings → Docker Engine 中配置 `registry-mirrors`，或在 Settings → Resources → Proxies 中设置代理。
Linux 在 `/etc/docker/daemon.json` 中配置后 `sudo systemctl restart docker`。

### 端口被占用（`port is already allocated`）

修改 `.env` 中的 `ASTRBOT_WEBUI_PORT` / `NAPCAT_WEBUI_PORT`，再 `docker compose up -d`。

### PowerShell 提示“无法加载脚本，因为在此系统上禁止运行脚本”

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### Linux：NapCat 报权限错误 / 登录态无法保存

`.env` 中 `NAPCAT_UID` / `NAPCAT_GID` 应与 `data/napcat` 所有者一致：

```bash
id -u; id -g                     # 写入 .env
sudo chown -R "$(id -u):$(id -g)" data/napcat
```

## 连接阶段（NapCat ⇄ AstrBot）

正常时 AstrBot 日志会出现：`aiocqhttp(OneBot v11) 适配器已连接。`

### 日志中没有“适配器已连接”

1. **NapCat 还没登录 QQ**：NapCat 只有登录后才会连接 AstrBot。打开 `http://127.0.0.1:6099/webui` 扫码。
2. **令牌不一致**：AstrBot 拒绝握手（缺失令牌 401，错误令牌 403），NapCat 日志会出现连接失败。运行：

   ```bash
   docker compose run --rm --no-deps init --astrbot-data /AstrBot/data --napcat-config /napcat/config --check
   ```

   不一致时执行 `docker compose up -d`（init 会同步），再 `docker compose restart astrbot napcat`。
   不要只在 NapCat WebUI 或 AstrBot WebUI 中单独修改令牌。
3. **地址错误**：NapCat 中 `astrbot` 客户端的地址必须是 `ws://astrbot:6199/ws`（不是 `127.0.0.1`，也不是容器 IP）。
   init 会自动纠正名为 `astrbot` 的客户端；如果你在 NapCat WebUI 里另建了客户端，请删除它或改为同样的地址与令牌。
4. **AstrBot 平台被禁用或端口改动**：WebUI「机器人」中确认 `napcat`（OneBot v11）已启用、端口 6199。
5. **网络**：`docker exec napcat bash -c 'exec 3<>/dev/tcp/astrbot/6199' && echo ok` 应输出 `ok`。
   失败时确认两个容器都在 `astrbot-qq_botnet` 网络：`docker network inspect astrbot-qq_botnet`。

### 频繁断开重连

- AstrBot 重启时 NapCat 会每 5 秒重试，属于正常现象。
- 持续断开：检查宿主机内存（QQNT 至少约 1 GB）、`docker compose ps` 中的重启次数、NapCat 日志中的崩溃信息。

## 登录阶段（QQ）

| 现象 | 处理 |
| --- | --- |
| WebUI 打不开 | 确认 `napcat` 在运行；地址是 `http://127.0.0.1:6099/webui`；远程服务器请用 SSH 隧道 |
| 不知道 WebUI 令牌 | `.env` 中的 `NAPCAT_WEBUI_TOKEN`；如为空，看 `docker compose logs napcat` |
| 二维码过期 | 在 WebUI 中刷新，或 `docker compose restart napcat` |
| 扫码后提示需要验证 / 登录失败 | 按手机 QQ 提示人工完成验证；频繁失败可能被风控，建议换网络环境稍后重试或更换账号。本项目不提供绕过手段 |
| 重启后要求重新扫码，日志出现“快速登录错误：登录态已失效” | QQ 把登录态与设备特征绑定。确认 `compose.yaml` 中 napcat 的 `hostname: napcat-qq` 与 `mac_address`（`NAPCAT_MAC_ADDRESS`）没有被删除或修改，且 `data/napcat/qq` 已持久化、有写权限；修改过这两项后需要重新扫码一次。QQ 侧也可能主动要求重新验证 |
| 扫码登录成了错误的账号 | 用正确账号重新扫码即可：`docker compose restart napcat` 后打开 WebUI 扫码。误登录账号的会话数据仍在 `data/napcat/`，可在该账号手机端「登录设备管理」中下线 |
| 版本过旧无法登录 | 更新 `NAPCAT_IMAGE` 到最新发布标签后 `docker compose pull && docker compose up -d` |

## 对话阶段

### 私聊不回复

1. `docker compose logs -f astrbot` 中是否出现 `[napcat(aiocqhttp)] 昵称/QQ号: 内容`：
   - 没有 → 消息没到 AstrBot，回到“连接阶段”；
   - 有 → 继续往下看。
2. 模型错误：日志中出现 401/403/404/超时等 → 检查 `LLM_API_KEY`、Base URL（通常以 `/v1` 结尾）、模型 ID；
   在 WebUI「模型提供商」中测试。修改 `.env` 后要 `docker compose up -d`。
3. 白名单：日志出现 `is not in the session allowlist` → 在「配置文件」白名单中加入该会话，或清空白名单。
4. 限流：日志出现 `[qq_guard] 用户 ... 触发限流` 或会话限流提示 → 调整插件 / 配置文件中的限流参数。
5. 没有可用模型：日志提示未找到提供商 → 在「模型提供商」中添加并启用。

### 自然接话太吵 / 太安静 / 完全不接话

- 太吵：调大 `natural_chat_min_new_messages`、`natural_chat_cooldown_seconds`，或调小 `natural_chat_max_replies_per_hour`；
- 太安静：调小 `natural_chat_min_new_messages`，或在 `natural_chat_name_triggers` 中加入大家对机器人的称呼；
- 完全不接话：确认群号在 `natural_chat_groups` 中（群白名单非空时也要在白名单中），在群里发 `/guard` 应显示
  `自然接话: 开启`；每次判断都会在日志中留下一行 `[qq_guard] 群 … 自然接话判断：接话/不接话`，没有这行说明还没满足判断条件（新消息条数、停顿、冷却），出现 `自然接话失败` 时查看后面的错误；
- 判断由模型完成，模型认为不适合时就不会接话，这是预期行为。

### 群里 @ 了也不回复

- 确认是 @ 了**机器人本身**（不是同名用户），或直接回复机器人的消息；
- qq_guard 的 `group_allowlist` 非空时，本群必须在列表中（群里发 `/guard` 查看）；
- AstrBot 会话白名单非空时，本群 UMO 或群号必须在列表中；
- 群聊普通消息、`@全体成员` 按设计不会回复。

### 回复了自己 / 两个机器人互相回复

本项目已在三处防止自回复（见 [架构说明](architecture.md#可靠性设计)）。如仍出现循环，通常是群里有**另一个**机器人：
在 qq_guard 中不要把该群加入白名单，或在 AstrBot「配置文件」中把对方 QQ 加入屏蔽/调整唤醒方式。

### 上下文混乱 / 想清空

- `/reset` 清空当前会话上下文，`/new` 新建对话；
- 群内希望每人独立上下文：开启 `unique_session`。

## WebUI

| 现象 | 处理 |
| --- | --- |
| 忘记 AstrBot 密码 | 在 `.env` 设置 `ASTRBOT_DASHBOARD_INITIAL_PASSWORD=新密码`（≥8 位），临时在 `compose.yaml` 的 astrbot `environment` 中加入 `ASTRBOT_RESET_DASHBOARD_PASSWORD: "1"`，执行 `docker compose up -d`；登录并修改密码后删除该变量再 `docker compose up -d` |
| 远程访问 | 保持 `127.0.0.1` 绑定，使用 `ssh -L 6185:127.0.0.1:6185 -L 6099:127.0.0.1:6099 user@server` |

## 收集信息求助

```bash
docker compose ps
docker compose logs --tail 200 astrbot napcat init > logs.txt
```

分享前请删除日志中的 QQ 号、令牌、二维码链接等敏感信息；不要分享 `.env` 和 `data/` 目录。
