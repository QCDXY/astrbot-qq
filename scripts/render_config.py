#!/usr/bin/env python3
"""根据环境变量生成/同步 AstrBot 与 NapCat 的 OneBot v11 连接配置。

由 compose.yaml 中的 ``init`` 服务在每次启动前运行（使用 AstrBot 镜像自带的 Python），
也可以在宿主机上直接运行。只依赖标准库。

行为约定：
- AstrBot ``data/cmd_config.json`` 不存在时写入一份“种子”配置（安全默认值 + OneBot 平台 +
  可选的 OpenAI 兼容模型），其余字段由 AstrBot 首次启动时自动补全。
- 配置已存在时只同步由本脚本管理的连接字段（平台 ``napcat`` 的监听地址/端口/令牌，
  NapCat 中名为 ``astrbot`` 的 WebSocket 客户端），不会覆盖用户在 WebUI 中的其他修改。
- NapCat 登录后会把 ``onebot11.json`` 复制为 ``onebot11_<QQ号>.json`` 并以后者为准，
  因此两类文件都会被同步。
- ``--check`` 只检查不写入，发现不一致时退出码为 1。

用法：
    python scripts/render_config.py --astrbot-data data/astrbot --napcat-config data/napcat/config
    python scripts/render_config.py --check ...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLATFORM_ID = "napcat"
WS_CLIENT_NAME = "astrbot"
WS_REVERSE_HOST = "0.0.0.0"
WS_REVERSE_PORT = 6199
DEFAULT_WS_URL = f"ws://astrbot:{WS_REVERSE_PORT}/ws"
LLM_SOURCE_ID = "llm"
LLM_PROVIDER_ID = "default_llm"
# 配置文件中只保存对环境变量的引用，AstrBot 加载模型时解析（provider.key 支持 "$ENV" 语法）
LLM_KEY_REF = "$LLM_API_KEY"
MIN_TOKEN_LENGTH = 16
PLACEHOLDER_PREFIXES = ("change-me", "changeme", "your-", "replace-")


class ConfigError(Exception):
    """输入不合法（例如缺少令牌），需要用户修正 .env。"""


@dataclass
class Settings:
    access_token: str
    ws_url: str = DEFAULT_WS_URL
    admin_ids: list[str] = field(default_factory=list)
    llm_base_url: str = ""
    llm_model: str = ""
    timezone: str = "Asia/Shanghai"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        token = env.get("ONEBOT_ACCESS_TOKEN", "").strip()
        validate_token(token)
        validate_webui_token(env.get("NAPCAT_WEBUI_TOKEN", "").strip())
        validate_dashboard_password(env.get("ASTRBOT_DASHBOARD_INITIAL_PASSWORD", ""))
        admins = [a.strip() for a in env.get("ADMIN_QQ", "").replace("，", ",").split(",")]
        return cls(
            access_token=token,
            ws_url=env.get("ONEBOT_WS_URL", "").strip() or DEFAULT_WS_URL,
            admin_ids=[a for a in admins if a],
            llm_base_url=env.get("LLM_BASE_URL", "").strip(),
            llm_model=env.get("LLM_MODEL", "").strip(),
            timezone=env.get("TZ", "").strip() or "Asia/Shanghai",
        )


def validate_token(token: str) -> None:
    if not token:
        raise ConfigError(
            "ONEBOT_ACCESS_TOKEN 为空。请在 .env 中设置一个随机长字符串"
            "（可运行 scripts/init-env.ps1 或 scripts/init-env.sh 自动生成）。"
        )
    if token.lower().startswith(PLACEHOLDER_PREFIXES):
        raise ConfigError("ONEBOT_ACCESS_TOKEN 仍是示例值，请替换为随机字符串。")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"ONEBOT_ACCESS_TOKEN 太短，至少需要 {MIN_TOKEN_LENGTH} 个字符。")
    if any(c.isspace() for c in token) or '"' in token:
        raise ConfigError("ONEBOT_ACCESS_TOKEN 不能包含空白字符或双引号。")


def validate_webui_token(token: str) -> None:
    """NapCat WebUI 令牌可以留空（NapCat 会随机生成并打印在日志里），但不能是示例值。"""
    if token and token.lower().startswith(PLACEHOLDER_PREFIXES):
        raise ConfigError("NAPCAT_WEBUI_TOKEN 仍是示例值，请替换为随机字符串或留空。")


def validate_dashboard_password(password: str) -> None:
    """与 AstrBot 的密码策略一致（>=8 位，含大写、小写字母和数字）。留空表示由 AstrBot 随机生成。"""
    if not password:
        return
    if (
        len(password) < 8
        or not re.search(r"[A-Z]", password)
        or not re.search(r"[a-z]", password)
        or not re.search(r"\d", password)
    ):
        raise ConfigError(
            "ASTRBOT_DASHBOARD_INITIAL_PASSWORD 不符合 AstrBot 密码要求："
            "至少 8 位，且包含大写字母、小写字母和数字；或留空由 AstrBot 随机生成。"
        )


# --------------------------------------------------------------------------- #
# AstrBot
# --------------------------------------------------------------------------- #


def platform_entry(settings: Settings) -> dict[str, Any]:
    return {
        "id": PLATFORM_ID,
        "type": "aiocqhttp",
        "enable": True,
        "ws_reverse_host": WS_REVERSE_HOST,
        "ws_reverse_port": WS_REVERSE_PORT,
        "ws_reverse_token": settings.access_token,
    }


def build_astrbot_seed(settings: Settings) -> dict[str, Any]:
    """首次部署时的 AstrBot 配置。未列出的字段由 AstrBot 用官方默认值补全。"""
    seed: dict[str, Any] = {
        "config_version": 5,
        "platform": [platform_entry(settings)],
        "platform_settings": {
            # 内置的会话级频率限制：每个会话 60 秒内最多 20 条，超出直接丢弃
            "rate_limit": {"time": 60, "count": 20, "strategy": "discard"},
            # 白名单开启但列表为空 = 不限制；在 WebUI 中填写会话 ID 后生效
            "enable_id_white_list": True,
            "id_whitelist": [],
            # 防止机器人回复自己的消息形成循环
            "ignore_bot_self_message": True,
            # 群聊只在被 @ 机器人、回复机器人或使用唤醒前缀时响应；@全体成员 不触发
            "ignore_at_all": True,
            "friend_message_needs_wake_prefix": False,
            "unique_session": False,
        },
        # 群聊上下文：记录群消息，机器人回复时带上自上次发言以来的群聊内容；
        # 只保留最近 50 条，避免长时间不说话后一次注入过多内容
        "provider_ltm_settings": {"group_icl_enable": True, "group_message_max_cnt": 50},
        "admins_id": settings.admin_ids or ["astrbot"],
        "wake_prefix": ["/"],
        "timezone": settings.timezone,
        # 日志写入 data/logs，便于排查
        "log_file_enable": True,
        "log_file_path": "logs/astrbot.log",
        "log_file_max_mb": 20,
    }
    if settings.llm_base_url and settings.llm_model:
        seed["provider_sources"] = [
            {
                "id": LLM_SOURCE_ID,
                "provider": "openai",
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
                "enable": True,
                "key": [LLM_KEY_REF],
                "api_base": settings.llm_base_url,
                "timeout": 120,
                "proxy": "",
                "custom_headers": {},
            }
        ]
        seed["provider"] = [
            {
                "id": LLM_PROVIDER_ID,
                "provider_source_id": LLM_SOURCE_ID,
                "model": settings.llm_model,
                "modalities": ["text"],
                "custom_extra_body": {},
                "enable": True,
            }
        ]
    return seed


def sync_astrbot_dict(conf: dict[str, Any], settings: Settings) -> list[str]:
    """把受管理的连接字段同步进已有配置，返回修改项说明（空列表表示无需修改）。"""
    changes: list[str] = []
    platforms = conf.get("platform")
    if not isinstance(platforms, list):
        platforms = []
        conf["platform"] = platforms
        changes.append("platform: 重建平台列表")

    target = next(
        (p for p in platforms if isinstance(p, dict) and p.get("id") == PLATFORM_ID),
        None,
    )
    if target is None:
        platforms.append(platform_entry(settings))
        changes.append(f"platform[{PLATFORM_ID}]: 新增 OneBot v11 平台")
    else:
        for key, value in platform_entry(settings).items():
            if key == "enable":
                continue  # 尊重用户在 WebUI 中禁用平台的选择
            if target.get(key) != value:
                target[key] = value
                changes.append(f"platform[{PLATFORM_ID}].{key}")

    ps = conf.setdefault("platform_settings", {})
    if isinstance(ps, dict) and ps.get("ignore_bot_self_message") is not True:
        ps["ignore_bot_self_message"] = True
        changes.append("platform_settings.ignore_bot_self_message")
    return changes


def sync_astrbot_config(path: Path, settings: Settings, write: bool = True) -> list[str]:
    if not path.exists():
        if write:
            write_json(path, build_astrbot_seed(settings))
        return [f"{path.name}: 新建种子配置"]
    conf = read_json(path)
    changes = sync_astrbot_dict(conf, settings)
    if changes and write:
        write_json(path, conf)
    return changes


# --------------------------------------------------------------------------- #
# NapCat
# --------------------------------------------------------------------------- #


def ws_client_entry(settings: Settings) -> dict[str, Any]:
    return {
        "enable": True,
        "name": WS_CLIENT_NAME,
        "url": settings.ws_url,
        "reportSelfMessage": False,
        "messagePostFormat": "array",
        "token": settings.access_token,
        "debug": False,
        "heartInterval": 30000,
        "reconnectInterval": 5000,
    }


def build_onebot11(settings: Settings) -> dict[str, Any]:
    """与 NapCat-Docker templates/astrbot.json 一致，仅填入令牌并缩短重连间隔。"""
    return {
        "network": {
            "httpServers": [],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [],
            "websocketClients": [ws_client_entry(settings)],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
    }


# 已有配置中强制保持的字段（连接正确性与防自回复循环）
_MANAGED_WS_KEYS = ("url", "token", "reportSelfMessage", "messagePostFormat")


def sync_onebot11_dict(conf: dict[str, Any], settings: Settings) -> list[str]:
    changes: list[str] = []
    network = conf.get("network")
    if not isinstance(network, dict):
        network = {}
        conf["network"] = network
    clients = network.get("websocketClients")
    if not isinstance(clients, list):
        clients = []
        network["websocketClients"] = clients
    target = next(
        (c for c in clients if isinstance(c, dict) and c.get("name") == WS_CLIENT_NAME),
        None,
    )
    if target is None:
        clients.append(ws_client_entry(settings))
        changes.append(f"websocketClients[{WS_CLIENT_NAME}]: 新增")
        return changes
    wanted = ws_client_entry(settings)
    for key in _MANAGED_WS_KEYS:
        if target.get(key) != wanted[key]:
            target[key] = wanted[key]
            changes.append(f"websocketClients[{WS_CLIENT_NAME}].{key}")
    return changes


def napcat_onebot_files(config_dir: Path) -> list[Path]:
    files = [config_dir / "onebot11.json"]
    files += sorted(p for p in config_dir.glob("onebot11_*.json") if p.is_file())
    return files


def sync_napcat_configs(config_dir: Path, settings: Settings, write: bool = True) -> list[str]:
    changes: list[str] = []
    for path in napcat_onebot_files(config_dir):
        if not path.exists():
            if write:
                write_json(path, build_onebot11(settings))
            changes.append(f"{path.name}: 新建")
            continue
        conf = read_json(path)
        file_changes = sync_onebot11_dict(conf, settings)
        if file_changes and write:
            write_json(path, conf)
        changes += [f"{path.name}: {c}" for c in file_changes]
    return changes


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 顶层必须是 JSON 对象")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    """原子写入，避免写到一半时容器启动读到损坏的文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def run(astrbot_data: Path, napcat_config: Path, check: bool, env: dict[str, str] | None = None) -> int:
    settings = Settings.from_env(env)
    astrbot_changes = sync_astrbot_config(astrbot_data / "cmd_config.json", settings, write=not check)
    napcat_changes = sync_napcat_configs(napcat_config, settings, write=not check)

    verb = "需要更新" if check else "已更新"
    for c in astrbot_changes:
        print(f"[astrbot] {verb}: {c}")
    for c in napcat_changes:
        print(f"[napcat]  {verb}: {c}")
    if not astrbot_changes and not napcat_changes:
        print("[ok] AstrBot 与 NapCat 的 OneBot 连接配置一致。")
    print(f"[info] NapCat -> {settings.ws_url}  (AstrBot 监听 {WS_REVERSE_HOST}:{WS_REVERSE_PORT})")
    return 1 if check and (astrbot_changes or napcat_changes) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--astrbot-data", type=Path, default=Path("data/astrbot"))
    parser.add_argument("--napcat-config", type=Path, default=Path("data/napcat/config"))
    parser.add_argument("--check", action="store_true", help="只检查，不写入")
    args = parser.parse_args(argv)
    try:
        return run(args.astrbot_data, args.napcat_config, args.check)
    except ConfigError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
