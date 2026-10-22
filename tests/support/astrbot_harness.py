"""在临时目录中以子进程方式运行真实的 AstrBot（pip 包，与镜像同版本），供集成测试使用。

配置完全由 scripts/render_config.py 生成（与 Docker 部署的 init 服务相同），
仅为避免端口冲突把监听端口改为随机空闲端口。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import render_config  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_qq_guard"
PLUGIN_SRC = PROJECT_ROOT / "plugins" / PLUGIN_NAME


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


@dataclass
class AstrBotProcess:
    root: Path
    env_vars: dict[str, str]
    plugin_config: dict | None = None
    ws_port: int = field(default_factory=free_port)
    dashboard_port: int = field(default_factory=free_port)
    proc: subprocess.Popen | None = None
    log_path: Path | None = None

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.ws_port}/ws"

    def prepare(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        (self.root / ".astrbot").touch()
        napcat_dir = self.root / "napcat_config"
        rc = render_config.run(self.data, napcat_dir, check=False, env=self.env_vars)
        assert rc == 0
        self.retarget_port()
        plugin_dst = self.data / "plugins" / PLUGIN_NAME
        if plugin_dst.exists():
            shutil.rmtree(plugin_dst)
        shutil.copytree(PLUGIN_SRC, plugin_dst, ignore=shutil.ignore_patterns("__pycache__"))
        if self.plugin_config is not None:
            cfg_dir = self.data / "config"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / f"{PLUGIN_NAME}_config.json").write_text(
                json.dumps(self.plugin_config, ensure_ascii=False), encoding="utf-8"
            )

    def retarget_port(self) -> None:
        path = self.data / "cmd_config.json"
        conf = json.loads(path.read_text(encoding="utf-8-sig"))
        for p in conf["platform"]:
            if p.get("id") == render_config.PLATFORM_ID:
                p["ws_reverse_host"] = "127.0.0.1"
                p["ws_reverse_port"] = self.ws_port
        path.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")

    def start(self, timeout: float = 180.0) -> None:
        env = os.environ.copy()
        env.update(
            {
                "LLM_API_KEY": self.env_vars.get("LLM_API_KEY", ""),
                "DASHBOARD_PORT": str(self.dashboard_port),
                "DASHBOARD_HOST": "127.0.0.1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
        )
        # 与 compose.yaml 中 astrbot 的启动命令一致：空密码时不传该变量，由 AstrBot 随机生成
        password = self.env_vars.get("ASTRBOT_DASHBOARD_INITIAL_PASSWORD", "Test-Passw0rd!")
        if password:
            env["ASTRBOT_DASHBOARD_INITIAL_PASSWORD"] = password
        else:
            env.pop("ASTRBOT_DASHBOARD_INITIAL_PASSWORD", None)
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            env.pop(k, None)
        self.log_path = self.root / f"astrbot-{int(time.time() * 1000)}.log"
        log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "astrbot.cli", "run"],
            cwd=self.root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"AstrBot 进程提前退出，日志：\n{self.read_log()[-4000:]}")
            if port_open(self.ws_port) and "qq_guard" in self.read_log():
                # 再给插件/平台一点时间完成初始化
                time.sleep(1.0)
                return
            time.sleep(0.5)
        raise TimeoutError(f"AstrBot 未在 {timeout}s 内就绪，日志：\n{self.read_log()[-4000:]}")

    def wait_for_history(self, text: str, timeout: float = 30.0) -> None:
        """等待某段文本写入会话历史（AstrBot 在回复发出后才异步保存历史）。"""
        db = self.data / "data_v4.db"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if db.exists():
                with sqlite3.connect(db) as conn:
                    rows = conn.execute("select content from conversations").fetchall()
                if any(text in json.dumps(json.loads(r[0] or "[]"), ensure_ascii=False) for r in rows):
                    return
            time.sleep(0.5)
        raise TimeoutError(f"会话历史中未出现 {text!r}")

    def read_log(self) -> str:
        if not self.log_path or not self.log_path.exists():
            return ""
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
