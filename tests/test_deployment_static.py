"""部署配置的静态测试：Compose 结构/安全默认值、.env 示例、脚本语法、密钥与敏感数据检查。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"
ENV_EXAMPLE = ROOT / ".env.example"

_VAR = re.compile(r"\$\{([A-Z0-9_]+)(?:(:?[-?])([^}]*))?\}")
_INTERP = re.compile(r"\$\$|" + _VAR.pattern)


def parse_env_file(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def interpolate(obj, env: dict[str, str]):
    """按 Compose 规则展开 ${VAR} / ${VAR:-default} / ${VAR-default} / ${VAR:?err}。"""
    if isinstance(obj, str):
        def repl(m: re.Match) -> str:
            if m.group(0) == "$$":  # 转义的字面量 $
                return "$"
            name, op, arg = m.group(1), m.group(2), m.group(3) or ""
            val = env.get(name)
            if op == ":-":
                return val if val else arg
            if op == "-":
                return arg if val is None else val
            if op in (":?", "?"):
                if not val:
                    raise ValueError(arg)
                return val
            return val or ""
        return _INTERP.sub(repl, obj)
    if isinstance(obj, list):
        return [interpolate(i, env) for i in obj]
    if isinstance(obj, dict):
        return {k: interpolate(v, env) for k, v in obj.items()}
    return obj


@pytest.fixture(scope="module")
def raw_compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose(raw_compose) -> dict:
    return interpolate(raw_compose, parse_env_file(ENV_EXAMPLE))


# ---------------------------------------------------------------- 项目结构


@pytest.mark.parametrize(
    "rel",
    [
        "compose.yaml",
        ".env.example",
        ".gitignore",
        ".gitattributes",
        "README.md",
        "docs/architecture.md",
        "docs/configuration.md",
        "docs/troubleshooting.md",
        "docs/acceptance-report.md",
        "scripts/render_config.py",
        "scripts/init-env.ps1",
        "scripts/init-env.sh",
        "scripts/start.ps1",
        "scripts/start.sh",
        "scripts/stop.ps1",
        "scripts/stop.sh",
        "scripts/doctor.ps1",
        "scripts/doctor.sh",
        "plugins/astrbot_plugin_qq_guard/main.py",
        "plugins/astrbot_plugin_qq_guard/metadata.yaml",
        "plugins/astrbot_plugin_qq_guard/_conf_schema.json",
        "LICENSE",
        "NOTICE",
        "tests/schemas/LICENSE",
        "tests/schemas/NOTICE",
    ],
)
def test_required_files_exist(rel):
    assert (ROOT / rel).is_file(), rel


# ---------------------------------------------------------------- Compose


def test_compose_matches_official_schema(raw_compose):
    schema = json.loads((ROOT / "tests" / "schemas" / "compose-spec.json").read_text(encoding="utf-8"))
    jsonschema.validate(raw_compose, schema)


def _compose_bin() -> list[str] | None:
    custom = os.environ.get("DOCKER_COMPOSE_BIN")
    if custom and Path(custom).exists():
        return [custom]
    docker = shutil.which("docker")
    if docker and subprocess.run([docker, "compose", "version"], capture_output=True).returncode == 0:
        return [docker, "compose"]
    legacy = shutil.which("docker-compose")
    return [legacy] if legacy else None


@pytest.mark.docker
def test_docker_compose_config_validates():
    """使用官方 docker compose CLI 校验（无需 Docker 引擎）。可用 DOCKER_COMPOSE_BIN 指定独立二进制。"""
    cmd = _compose_bin()
    if not cmd:
        pytest.skip("未找到 docker compose；可设置 DOCKER_COMPOSE_BIN 指向 docker-compose 可执行文件")
    res = subprocess.run(
        [*cmd, "--env-file", str(ENV_EXAMPLE), "-f", str(COMPOSE), "config", "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
    )
    assert res.returncode == 0, res.stderr
    cfg = json.loads(res.stdout)
    assert set(cfg["services"]) == {"init", "astrbot", "napcat"}
    for svc in ("astrbot", "napcat"):
        for p in cfg["services"][svc].get("ports", []):
            assert p["host_ip"] == "127.0.0.1"
            assert str(p["target"]) != "6199"


@pytest.mark.skipif(not shutil.which("bash") and not Path(r"C:\Program Files\Gitinash.exe").exists(),
                    reason="无 bash")
@pytest.mark.parametrize(("password", "expect_set"), [("", False), ("Valid-Passw0rd!", True)])
def test_astrbot_command_unsets_empty_initial_password(compose, password, expect_set):
    """AstrBot 遇到空字符串密码会拒绝启动；启动命令必须在为空时 unset 该变量。"""
    cmd = compose["services"]["astrbot"]["command"]
    assert cmd[:2] == ["sh", "-c"]
    script = cmd[2]
    assert script.endswith("exec python main.py"), "其余行为应与镜像默认 CMD 一致"
    probe = script.replace("exec python main.py", 'echo "set=${ASTRBOT_DASHBOARD_INITIAL_PASSWORD+yes}"')
    env = {**os.environ, "ASTRBOT_DASHBOARD_INITIAL_PASSWORD": password}
    res = subprocess.run([_bash(), "-c", probe], capture_output=True, text=True, encoding="utf-8", env=env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == ("set=yes" if expect_set else "set=")


def test_services_and_pinned_images(compose):
    services = compose["services"]
    assert set(services) == {"init", "astrbot", "napcat"}
    for name, svc in services.items():
        image = svc["image"]
        assert ":" in image and not image.endswith(":latest"), f"{name} 镜像应固定版本：{image}"
    assert services["init"]["image"] == services["astrbot"]["image"]
    assert services["astrbot"]["image"].startswith("soulter/astrbot:")
    assert services["napcat"]["image"].startswith("mlikiowa/napcat-docker:")


def test_shared_network_and_service_name_resolution(compose):
    services = compose["services"]
    assert services["astrbot"]["networks"] == ["botnet"]
    assert list(services["napcat"]["networks"]) == ["botnet"]
    assert compose["networks"]["botnet"]["driver"] == "bridge"
    for svc in ("astrbot", "napcat"):
        assert "network_mode" not in services[svc]
        assert "ipv4_address" not in json.dumps(services[svc])
    init_env = services["init"]["environment"]
    assert init_env["ONEBOT_WS_URL"] == "ws://astrbot:6199/ws"


def test_napcat_has_fixed_device_identity(compose):
    """QQ 登录态与设备特征绑定：容器重建后 MAC 或主机名变化会导致“登录态已失效”，必须固定。"""
    hostname = compose["services"]["napcat"]["hostname"]
    # NapCat 入口脚本会尝试重命名这类主机名（容器内无权限而失败），所以必须避开
    assert not re.search(r"docker|container|lxc", hostname) and not re.fullmatch(r"[a-f0-9]{12,}", hostname)
    mac = compose["services"]["napcat"]["networks"]["botnet"]["mac_address"]
    assert re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac)
    assert int(mac[:2], 16) & 0b10, "应使用本地管理地址（第一个字节的第 2 位为 1）"
    assert not int(mac[:2], 16) & 0b1, "不能是组播地址"


def test_ports_bound_to_localhost_and_onebot_not_published(compose):
    def ports(svc):
        return compose["services"][svc].get("ports", [])

    assert ports("astrbot") == ["127.0.0.1:6185:6185"]
    assert ports("napcat") == ["127.0.0.1:6099:6099"]
    assert "ports" not in compose["services"]["init"]
    assert compose["services"]["init"]["network_mode"] == "none"
    assert "6199" in [str(p) for p in compose["services"]["astrbot"]["expose"]]


def test_restart_policy_and_startup_order(compose):
    s = compose["services"]
    assert s["astrbot"]["restart"] == s["napcat"]["restart"] == "unless-stopped"
    assert s["init"]["restart"] == "no"
    assert s["astrbot"]["depends_on"]["init"]["condition"] == "service_completed_successfully"
    assert s["napcat"]["depends_on"]["init"]["condition"] == "service_completed_successfully"
    assert s["napcat"]["depends_on"]["astrbot"]["condition"] == "service_started"
    assert "healthcheck" in s["astrbot"]


def test_persistent_volumes(compose):
    def mounts(svc):
        return {v.split(":")[1]: v.split(":")[0] for v in compose["services"][svc]["volumes"]}

    a, n = mounts("astrbot"), mounts("napcat")
    assert a["/AstrBot/data"] == "./data/astrbot"
    assert n["/app/napcat/config"] == "./data/napcat/config"
    assert n["/app/.config/QQ"] == "./data/napcat/qq"
    assert a["/AstrBot/data/plugins/astrbot_plugin_qq_guard"] == "./plugins/astrbot_plugin_qq_guard"
    i = mounts("init")
    assert i["/AstrBot/data"] == a["/AstrBot/data"]
    assert i["/napcat/config"] == n["/app/napcat/config"]


def test_secrets_only_reach_the_services_that_need_them(raw_compose):
    s = raw_compose["services"]
    assert "LLM_API_KEY" in s["astrbot"]["environment"]
    assert "LLM_API_KEY" not in s["napcat"]["environment"]
    assert "LLM_API_KEY" not in s["init"]["environment"], "API Key 不应写入配置文件"
    assert "ONEBOT_ACCESS_TOKEN" not in s["napcat"]["environment"]
    # 所有值都来自变量引用，compose 文件本身不含密钥
    for svc in s.values():
        for key, val in (svc.get("environment") or {}).items():
            if any(w in key for w in ("TOKEN", "KEY", "PASSWORD")):
                assert str(val).startswith("${"), f"{key} 应引用 .env"


def test_env_example_covers_all_compose_variables(raw_compose):
    used = set(_VAR.findall(COMPOSE.read_text(encoding="utf-8")))
    used_names = {u[0] for u in used}
    example = parse_env_file(ENV_EXAMPLE)
    assert used_names <= set(example), used_names - set(example)


def test_env_example_has_no_real_secrets():
    example = parse_env_file(ENV_EXAMPLE)
    assert example["LLM_API_KEY"] == ""
    assert example["ASTRBOT_DASHBOARD_INITIAL_PASSWORD"] == ""
    assert example["ONEBOT_ACCESS_TOKEN"].startswith("change-me")
    assert example["NAPCAT_WEBUI_TOKEN"].startswith("change-me")
    assert example["WEBUI_BIND_HOST"] == "127.0.0.1"


def test_no_hardcoded_container_ips():
    ip = re.compile(r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(\d{1,3}\.){3}\d{1,3}\b")
    for rel in ("compose.yaml", "scripts/render_config.py", ".env.example"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not ip.search(text), f"{rel} 含写死的 IP：{ip.search(text).group(0)}"


# ---------------------------------------------------------------- 密钥与敏感数据


def _project_files() -> list[Path]:
    """仓库中会被提交的文件（已跟踪 + 未被忽略的新文件）。"""
    if shutil.which("git") and (ROOT / ".git").exists():
        res = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT, capture_output=True, check=True,
        )
        return [ROOT / p for p in res.stdout.decode("utf-8").split("\0") if p]
    skip = {".git", ".venv", "data", ".pytest_cache", ".pytest_tmp", "__pycache__"}
    return [p for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip)]


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
]


def test_gitignore_excludes_sensitive_paths():
    if not (shutil.which("git") and (ROOT / ".git").exists()):
        pytest.skip("不是 git 仓库")
    paths = [".env", ".env.local", "data/astrbot/cmd_config.json", "data/napcat/qq/x", "data/napcat/config/onebot11.json",
             "qrcode.png", "logs/a.log", ".venv/x"]
    res = subprocess.run(["git", "check-ignore", "--no-index", *paths], cwd=ROOT, capture_output=True, text=True)
    ignored = set(res.stdout.split())
    assert ignored == set(paths), set(paths) - ignored
    res = subprocess.run(["git", "check-ignore", "--no-index", ".env.example"], cwd=ROOT, capture_output=True)
    assert res.returncode == 1, ".env.example 应被提交"


def test_no_secrets_or_runtime_data_in_repository():
    files = _project_files()
    assert files
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        assert not rel.startswith(("data/", ".env", "logs/")) or rel == ".env.example", rel
        assert not re.search(r"(qrcode.*\.png|\.db|\.db-wal|\.log)$", rel), rel
        if p.suffix in {".png", ".jpg", ".exe", ".zip"} or not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for pat in SECRET_PATTERNS:
            m = pat.search(text)
            assert not m, f"{rel} 疑似包含密钥：{m.group(0)[:12]}..."


# ---------------------------------------------------------------- 脚本


def _bash() -> str | None:
    """返回可用的 bash；Windows 上跳过 System32 中的 WSL 启动器（路径语义不同），优先 Git Bash。"""
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
    return str(git_bash) if git_bash.exists() else None


@pytest.mark.skipif(not _bash(), reason="无 bash")
@pytest.mark.parametrize("name", ["init-env.sh", "start.sh", "stop.sh", "doctor.sh"])
def test_shell_scripts_syntax(name):
    res = subprocess.run([_bash(), "-n", (ROOT / "scripts" / name).as_posix()], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert b"\r\n" not in (ROOT / "scripts" / name).read_bytes(), "shell 脚本必须是 LF 换行"


def _powershells() -> list[str]:
    return [p for p in (shutil.which("pwsh"), shutil.which("powershell")) if p]


@pytest.mark.skipif(not _powershells(), reason="无 PowerShell")
@pytest.mark.parametrize("name", ["init-env.ps1", "start.ps1", "stop.ps1", "doctor.ps1"])
def test_powershell_scripts_parse(name):
    path = ROOT / "scripts" / name
    assert path.read_bytes().startswith(b"\xef\xbb\xbf"), "需要 UTF-8 BOM 以便 Windows PowerShell 5.1 正确读取中文"
    cmd = (
        "$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}',[ref]$t,[ref]$e);if($e.Count){{$e|%{{$_.Message}};exit 1}}"
    )
    for exe in _powershells():
        res = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True)
        assert res.returncode == 0, f"{exe}: {res.stdout}{res.stderr}"


def _copy_for_init(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy(ENV_EXAMPLE, proj / ".env.example")
    for f in ("init-env.ps1", "init-env.sh", "render_config.py"):
        shutil.copy(ROOT / "scripts" / f, proj / "scripts" / f)
    return proj


def _assert_generated_env(proj: Path) -> None:
    env = parse_env_file(proj / ".env")
    assert re.fullmatch(r"[0-9a-f]{48}", env["ONEBOT_ACCESS_TOKEN"])
    assert re.fullmatch(r"[0-9a-f]{32}", env["NAPCAT_WEBUI_TOKEN"])
    assert (proj / "data" / "napcat" / "qq").is_dir()
    # 生成的 .env 能被 init 接受
    sys.path.insert(0, str(ROOT / "scripts"))
    import render_config

    render_config.Settings.from_env(env)


@pytest.mark.skipif(not _powershells(), reason="无 PowerShell")
def test_init_env_ps1_generates_random_tokens_and_never_overwrites(tmp_path):
    proj = _copy_for_init(tmp_path)
    exe = _powershells()[0]
    script = str(proj / "scripts" / "init-env.ps1")
    run = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script]
    assert subprocess.run(run, capture_output=True).returncode == 0
    _assert_generated_env(proj)
    assert not (proj / ".env").read_bytes().startswith(b"\xef\xbb\xbf"), ".env 不应带 BOM"
    first = (proj / ".env").read_bytes()
    assert subprocess.run(run, capture_output=True).returncode == 0
    assert (proj / ".env").read_bytes() == first


@pytest.mark.skipif(not _bash(), reason="无 bash")
def test_init_env_sh_generates_random_tokens_and_never_overwrites(tmp_path):
    proj = _copy_for_init(tmp_path)
    run = [_bash(), (proj / "scripts" / "init-env.sh").as_posix()]
    res = subprocess.run(run, capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    _assert_generated_env(proj)
    first = (proj / ".env").read_bytes()
    assert subprocess.run(run, capture_output=True).returncode == 0
    assert (proj / ".env").read_bytes() == first


# ---------------------------------------------------------------- 文档


def test_readme_covers_the_whole_journey():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    must = [
        "docker compose up -d", "docker compose down", "scripts\\start.ps1", "./scripts/start.sh",
        "127.0.0.1:6099/webui", "127.0.0.1:6185", "扫码", "LLM_API_KEY", "ws://astrbot:6199/ws",
        "ONEBOT_ACCESS_TOKEN", "适配器已连接", "/help", "@", "待人工联调", "风险",
    ]
    missing = [m for m in must if m not in readme]
    assert not missing, missing
