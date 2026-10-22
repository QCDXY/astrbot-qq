# 验收报告

- 日期：2026-10-22
- 项目路径：仓库根目录
- 验证对象版本：AstrBot `4.29.1`（镜像 `soulter/astrbot:v4.29.1`，测试使用同版本 PyPI 包），NapCat 镜像 `mlikiowa/napcat-docker:v4.18.41`
- 验证环境：Windows 11，Python 3.12（uv 虚拟环境），pytest 9.1.1，Git Bash，PowerShell 7 与 Windows PowerShell 5.1，
  Docker Compose 独立二进制 v5.6.1（官方 GitHub Release，SHA-256 已校验）

## 1. 环境检查结论

| 项目 | 结果 |
| --- | --- |
| Docker 引擎 | **不可用**：Windows 上未安装 Docker Desktop；WSL Ubuntu 中也没有 Docker，且没有免密 sudo，无法自行安装 |
| Docker Compose CLI | 使用官方独立二进制做 `config` 校验（不需要 Docker 引擎） |
| Python / uv / Git | 可用 |

因此“实际启动容器”这一项**未执行**，标记为待人工联调；其余验收项均已通过自动化测试验证。

## 2. 自动化测试结果

命令：`uv run pytest`（设置了 `DOCKER_COMPOSE_BIN` 以启用 `docker compose config` 测试）

| 测试文件 | 类型 | 通过 | 失败 | 跳过 |
| --- | --- | --- | --- | --- |
| `tests/test_deployment_static.py` | 静态（Compose、密钥、脚本、文档） | 47 | 0 | 0 |
| `tests/test_render_config.py` | 单元（配置生成/同步） | 27 | 0 | 0 |
| `tests/test_guard_logic.py` | 单元（qq_guard 逻辑） | 14 | 0 | 0 |
| `tests/integration/test_message_flow.py` | 集成（真实 AstrBot + 模拟 NapCat/LLM） | 15 | 0 | 0 |
| `tests/integration/test_persistence.py` | 集成（重启恢复、模型故障、无初始密码启动） | 3 | 0 | 0 |
| **合计** | | **106** | **0** | **0** |

集成测试耗时约 2 分 15 秒。另外 `ruff check .` 通过；`bash -n` 与 PowerShell 5.1/7 语法解析全部通过。

### 集成测试覆盖的关键消息处理逻辑

真实运行的是 AstrBot 4.29.1 进程，配置由 `scripts/render_config.py` 生成（与 Docker 中 init 服务相同），插件为本项目
`qq_guard`。NapCat 由 OneBot v11 反向 WebSocket 客户端模拟，模型由 OpenAI 兼容模拟服务代替（回复为确定性的
`MOCK_REPLY#n`，**不是真实模型输出**）。

| 场景 | 结果 |
| --- | --- |
| 私聊消息 → 调用模型 → `send_private_msg` 回复；API Key 通过 `$LLM_API_KEY` 环境变量引用注入 | 通过 |
| 群聊未 @ 机器人 → 不回复、不调用模型 | 通过 |
| 群聊 @ 机器人 → `send_group_msg` 回复 | 通过 |
| 同一会话携带历史上下文；不同会话互不可见 | 通过 |
| `/help`、`/sid` 内置指令（不调用模型） | 通过 |
| `/guard` 插件指令显示群号与白名单状态 | 通过 |
| 群白名单外的群 @ 机器人 → 忽略；管理员不受限 | 通过 |
| 按用户限流：超限提示一次、不再调用模型、其他用户不受影响 | 通过 |
| 机器人自己的消息（群聊、私聊）→ 忽略，防止循环 | 通过 |
| 错误令牌握手返回 403；缺失令牌返回 401 | 通过 |
| 断线后重新连接，消息继续正常处理 | 通过 |
| AstrBot WebUI 返回 200 | 通过 |
| 重启后：WebUI 中的配置修改保留、init 再次运行不覆盖、对话历史保留、插件配置保留、密码哈希不变 | 通过 |
| 模型 API Key 错误 → 不产生伪造的成功回复 | 通过 |
| `ASTRBOT_DASHBOARD_INITIAL_PASSWORD` 留空 → AstrBot 正常启动并随机生成初始密码 | 通过 |

## 3. 验收标准逐项结论

| 验收标准（PLAN.md §8） | 结论 | 依据 |
| --- | --- | --- |
| 项目结构完整，配置文件和文档齐全 | 通过 | `test_required_files_exist`；README、docs/、scripts/、tests/、plugins/ |
| Docker Compose 配置校验通过 | 通过 | 官方 Compose JSON Schema 校验 + `docker compose config`（v5.6.1）校验 |
| Docker 可用时 AstrBot 和 NapCat 容器可以启动 | **未执行（待人工联调）** | 本机无 Docker 引擎。启动命令、入口、卷路径已对照两个镜像的官方 Dockerfile / entrypoint 源码核对 |
| 两个服务的网络配置及 WebSocket 目标地址正确 | 通过 | 同一 `botnet` 网络、服务名 `astrbot`、`ws://astrbot:6199/ws`、6199 不映射宿主机；静态测试 + 配置单元测试 |
| 关键消息处理逻辑通过模拟测试 | 通过 | 第 2 节集成测试 |
| 配置持久化、重启恢复、合理的安全默认值 | 通过 | 卷映射静态测试；重启集成测试；WebUI 仅绑定 127.0.0.1、令牌校验、`no-new-privileges`、日志轮转、`restart: unless-stopped` |
| 没有真实密钥或 QQ 凭据写入仓库 | 通过 | `.gitignore` 检查、仓库文件密钥扫描、`.env.example` 只含占位；API Key 不写入任何配置文件 |
| README 说明从启动到扫码、模型配置、WebSocket 连接、首次对话的全部操作 | 通过 | README §2.1–2.7；`test_readme_covers_the_whole_journey` |
| 所有实际执行的测试均有明确结果 | 通过 | 本报告第 2 节 |

## 4. 开发过程中发现并修复的问题

1. **空初始密码导致 AstrBot 无法启动**：Compose 会把未填写的 `ASTRBOT_DASHBOARD_INITIAL_PASSWORD` 以空字符串传入，
   AstrBot 对其校验时抛出 `Password cannot be empty`。修复：astrbot 服务启动命令在该变量为空时先 `unset`；
   init 提前按 AstrBot 的规则校验密码强度。新增静态测试（在 bash 中执行启动命令）与集成测试。
2. **NapCat 按账号的配置文件**：NapCat 登录后使用 `onebot11_<QQ号>.json`，只改 `onebot11.json` 会导致令牌更换后两端不一致。
   修复：init 同步所有 `onebot11*.json`（单元测试覆盖）。
3. **示例令牌被直接使用**：`.env.example` 中的占位令牌会让 NapCat WebUI 使用公开已知的令牌。修复：init 拒绝示例值，
   `init-env` 脚本生成随机令牌。
4. **AstrBot 配置文件带 UTF-8 BOM**：读取时统一使用 `utf-8-sig`。
5. 测试发现 AstrBot 在回复发出后才异步保存对话历史；重启测试改为确认历史已落库后再停止进程。

## 5. 待人工联调（需要用户授权/凭据）

以下项目**未通过、也未声称通过**，需要在装有 Docker 的机器上、使用真实 QQ 账号与真实 API Key 完成：

1. `docker compose up -d`：init 退出码 0，astrbot 变为 healthy，napcat 运行中；
2. NapCat WebUI（`http://127.0.0.1:6099/webui`）扫码登录；
3. AstrBot 日志出现 `aiocqhttp(OneBot v11) 适配器已连接。`，`scripts/doctor.*` 全部通过；
4. AstrBot WebUI「模型提供商」中测试真实模型连通；
5. 私聊收到真实模型回复；群里 @ 机器人收到回复、不 @ 不回复；
6. `docker compose restart` 后无需重新扫码，且上下文仍在。

## 6. 启动与停止

```powershell
# Windows PowerShell
.\scripts\init-env.ps1     # 首次：生成 .env，然后编辑模型配置
.\scripts\start.ps1        # 启动（等同 docker compose up -d）
.\scripts\doctor.ps1       # 诊断
.\scripts\stop.ps1         # 停止（等同 docker compose down，数据保留）
```

```bash
# Linux
./scripts/init-env.sh
./scripts/start.sh
./scripts/doctor.sh
./scripts/stop.sh
```
