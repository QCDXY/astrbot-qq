"""scripts/render_config.py 单元测试：两端令牌/地址一致、安全默认值、不覆盖用户修改。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import render_config as rc  # noqa: E402

TOKEN = "unit-test-token-0123456789"


def env(**over: str) -> dict[str, str]:
    base = {
        "ONEBOT_ACCESS_TOKEN": TOKEN,
        "LLM_BASE_URL": "https://example.invalid/v1",
        "LLM_MODEL": "some-model",
        "ADMIN_QQ": "12345, 67890",
    }
    base.update(over)
    return base


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8-sig"))


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "astrbot", tmp_path / "napcat"


# ---- 输入校验 ----


@pytest.mark.parametrize(
    "token",
    ["", "change-me-to-a-long-random-string", "short", "has space 0123456789abc", 'quote"0123456789abcdef'],
)
def test_rejects_bad_tokens(token):
    with pytest.raises(rc.ConfigError):
        rc.Settings.from_env(env(ONEBOT_ACCESS_TOKEN=token))


def test_rejects_placeholder_webui_token():
    with pytest.raises(rc.ConfigError):
        rc.Settings.from_env(env(NAPCAT_WEBUI_TOKEN="change-me-napcat-webui-token"))
    rc.Settings.from_env(env(NAPCAT_WEBUI_TOKEN=""))  # 留空允许


PASSWORD_CASES = ["", "Abcdefg1", "short1A", "alllower1", "ALLUPPER1", "NoDigitsHere", "Valid-Passw0rd!"]

_ASTRBOT_POLICY = """
import json, sys
from astrbot.core.utils.auth_password import validate_dashboard_password
out = {}
for p in json.loads(sys.argv[1]):
    try:
        validate_dashboard_password(p)
        out[p] = True
    except ValueError:
        out[p] = False
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def astrbot_password_verdicts(tmp_path_factory):
    """在子进程中调用 AstrBot 自己的校验函数（导入 astrbot.core 会在 ASTRBOT_ROOT 下生成 data/，故隔离到临时目录）。"""
    pytest.importorskip("astrbot")
    root = tmp_path_factory.mktemp("astrbot_root")
    res = subprocess.run(
        [sys.executable, "-c", _ASTRBOT_POLICY, json.dumps(PASSWORD_CASES)],
        cwd=root, env={**os.environ, "ASTRBOT_ROOT": str(root), "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8",
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("password", PASSWORD_CASES)
def test_dashboard_password_policy_matches_astrbot(password, astrbot_password_verdicts):
    """init 提前校验的规则必须与 AstrBot 自身一致，否则 AstrBot 会在启动时崩溃。"""
    try:
        rc.validate_dashboard_password(password)
        ours_ok = True
    except rc.ConfigError:
        ours_ok = False
    if password == "":
        assert ours_ok, "留空表示随机生成（compose 会 unset 该变量）"
        assert astrbot_password_verdicts[""] is False, "AstrBot 拒绝空字符串，因此 compose 必须 unset 它"
        return
    assert ours_ok == astrbot_password_verdicts[password]


def test_admin_ids_parsing():
    s = rc.Settings.from_env(env(ADMIN_QQ=" 1，2 , ,3"))
    assert s.admin_ids == ["1", "2", "3"]
    assert rc.Settings.from_env(env(ADMIN_QQ="")).admin_ids == []


# ---- 首次生成 ----


def test_first_run_creates_matching_configs(dirs):
    a, n = dirs
    assert rc.run(a, n, check=False, env=env()) == 0
    conf = load(a / "cmd_config.json")
    ob = load(n / "onebot11.json")

    platform = next(p for p in conf["platform"] if p["id"] == rc.PLATFORM_ID)
    client = next(c for c in ob["network"]["websocketClients"] if c["name"] == rc.WS_CLIENT_NAME)

    # 两端令牌一致
    assert platform["ws_reverse_token"] == client["token"] == TOKEN
    # AstrBot 作为服务端监听 0.0.0.0:6199，NapCat 用服务名连接（不写死 IP）
    assert platform["type"] == "aiocqhttp"
    assert (platform["ws_reverse_host"], platform["ws_reverse_port"]) == ("0.0.0.0", 6199)
    assert client["url"] == "ws://astrbot:6199/ws"
    assert client["enable"] is True
    assert client["reportSelfMessage"] is False
    assert client["messagePostFormat"] == "array"
    assert 0 < client["reconnectInterval"] <= 30000
    # NapCat 不开启其他网络入口
    for key in ("httpServers", "httpSseServers", "httpClients", "websocketServers"):
        assert ob["network"][key] == []


def test_seed_has_safe_defaults(dirs):
    a, n = dirs
    rc.run(a, n, check=False, env=env())
    conf = load(a / "cmd_config.json")
    ps = conf["platform_settings"]
    assert ps["ignore_bot_self_message"] is True
    assert ps["ignore_at_all"] is True
    assert ps["friend_message_needs_wake_prefix"] is False
    assert ps["rate_limit"]["count"] > 0 and ps["rate_limit"]["time"] > 0
    assert conf["admins_id"] == ["12345", "67890"]
    assert conf["wake_prefix"] == ["/"]
    assert conf["log_file_enable"] is True
    assert conf["config_version"] == 5
    assert conf["provider_ltm_settings"] == {"group_icl_enable": True, "group_message_max_cnt": 50}
    assert "dashboard" not in conf  # 由 AstrBot 生成随机密码，不在种子中设置


def test_seed_llm_uses_env_reference_not_secret(dirs):
    a, n = dirs
    rc.run(a, n, check=False, env=env(LLM_API_KEY="LEAK-CANARY-api-key-value"))
    text = (a / "cmd_config.json").read_text(encoding="utf-8")
    assert "LEAK-CANARY-api-key-value" not in text
    conf = json.loads(text)
    (source,) = conf["provider_sources"]
    (provider,) = conf["provider"]
    assert source["key"] == ["$LLM_API_KEY"]
    assert source["api_base"] == "https://example.invalid/v1"
    assert source["type"] == "openai_chat_completion"
    assert provider["provider_source_id"] == source["id"]
    assert provider["model"] == "some-model"


def test_no_llm_seed_when_not_configured(dirs):
    a, n = dirs
    rc.run(a, n, check=False, env=env(LLM_BASE_URL="", LLM_MODEL=""))
    conf = load(a / "cmd_config.json")
    assert "provider" not in conf and "provider_sources" not in conf


# ---- 再次运行：只同步受管理字段 ----


def test_rerun_is_idempotent(dirs, capsys):
    a, n = dirs
    rc.run(a, n, check=False, env=env())
    before = ((a / "cmd_config.json").read_bytes(), (n / "onebot11.json").read_bytes())
    assert rc.run(a, n, check=False, env=env()) == 0
    after = ((a / "cmd_config.json").read_bytes(), (n / "onebot11.json").read_bytes())
    assert before == after
    assert "[ok]" in capsys.readouterr().out


def test_token_rotation_updates_both_ends_and_keeps_user_edits(dirs):
    a, n = dirs
    rc.run(a, n, check=False, env=env())
    # 模拟 AstrBot 补全后的配置 + 用户在 WebUI 中的修改
    conf = load(a / "cmd_config.json")
    conf["platform_settings"]["reply_prefix"] = "[bot]"
    conf["provider"][0]["model"] = "user-picked-model"
    conf["platform"].append({"id": "other", "type": "telegram", "enable": True})
    (a / "cmd_config.json").write_text("﻿" + json.dumps(conf, ensure_ascii=False), encoding="utf-8")
    ob = load(n / "onebot11.json")
    ob["network"]["websocketClients"][0]["reconnectInterval"] = 12345
    ob["musicSignUrl"] = "https://user.example"
    (n / "onebot11.json").write_text(json.dumps(ob), encoding="utf-8")

    new = "rotated-token-abcdefghijklmnop"
    rc.run(a, n, check=False, env=env(ONEBOT_ACCESS_TOKEN=new, LLM_MODEL="ignored-after-first-run"))

    conf = load(a / "cmd_config.json")
    ob = load(n / "onebot11.json")
    assert next(p for p in conf["platform"] if p["id"] == "napcat")["ws_reverse_token"] == new
    assert ob["network"]["websocketClients"][0]["token"] == new
    assert conf["platform_settings"]["reply_prefix"] == "[bot]"
    assert conf["provider"][0]["model"] == "user-picked-model"
    assert any(p["id"] == "other" for p in conf["platform"])
    assert ob["network"]["websocketClients"][0]["reconnectInterval"] == 12345
    assert ob["musicSignUrl"] == "https://user.example"


def test_respects_user_disabling_platform(dirs):
    a, n = dirs
    rc.run(a, n, check=False, env=env())
    conf = load(a / "cmd_config.json")
    conf["platform"][0]["enable"] = False
    (a / "cmd_config.json").write_text(json.dumps(conf), encoding="utf-8")
    rc.run(a, n, check=False, env=env())
    assert load(a / "cmd_config.json")["platform"][0]["enable"] is False


def test_reenforces_self_message_guard(dirs):
    a, n = dirs
    rc.run(a, n, check=False, env=env())
    conf = load(a / "cmd_config.json")
    conf["platform_settings"]["ignore_bot_self_message"] = False
    (a / "cmd_config.json").write_text(json.dumps(conf), encoding="utf-8")
    ob = load(n / "onebot11.json")
    ob["network"]["websocketClients"][0]["reportSelfMessage"] = True
    (n / "onebot11.json").write_text(json.dumps(ob), encoding="utf-8")
    rc.run(a, n, check=False, env=env())
    assert load(a / "cmd_config.json")["platform_settings"]["ignore_bot_self_message"] is True
    assert load(n / "onebot11.json")["network"]["websocketClients"][0]["reportSelfMessage"] is False


def test_syncs_per_account_napcat_config(dirs):
    """NapCat 登录后使用 onebot11_<QQ号>.json，令牌同样需要同步。"""
    a, n = dirs
    rc.run(a, n, check=False, env=env())
    per_account = n / "onebot11_123456.json"
    ob = load(n / "onebot11.json")
    ob["network"]["websocketClients"][0]["token"] = "stale"
    ob["network"]["websocketClients"][0]["url"] = "ws://172.17.0.2:6199/ws"
    per_account.write_text(json.dumps(ob), encoding="utf-8")
    rc.run(a, n, check=False, env=env())
    client = load(per_account)["network"]["websocketClients"][0]
    assert client["token"] == TOKEN
    assert client["url"] == "ws://astrbot:6199/ws"


def test_adds_missing_client_and_platform(dirs):
    a, n = dirs
    n.mkdir(parents=True)
    a.mkdir(parents=True)
    (a / "cmd_config.json").write_text(json.dumps({"platform": []}), encoding="utf-8")
    (n / "onebot11.json").write_text(json.dumps({"network": {"websocketClients": []}}), encoding="utf-8")
    rc.run(a, n, check=False, env=env())
    assert load(a / "cmd_config.json")["platform"][0]["id"] == "napcat"
    assert load(n / "onebot11.json")["network"]["websocketClients"][0]["name"] == "astrbot"


# ---- --check 与命令行 ----


def test_check_mode_detects_mismatch_without_writing(dirs):
    a, n = dirs
    assert rc.run(a, n, check=True, env=env()) == 1  # 尚未初始化
    assert not (a / "cmd_config.json").exists()
    rc.run(a, n, check=False, env=env())
    assert rc.run(a, n, check=True, env=env()) == 0
    assert rc.run(a, n, check=True, env=env(ONEBOT_ACCESS_TOKEN="another-token-0123456789")) == 1
    assert load(n / "onebot11.json")["network"]["websocketClients"][0]["token"] == TOKEN


def test_invalid_json_reports_error(dirs):
    a, n = dirs
    a.mkdir(parents=True)
    (a / "cmd_config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(rc.ConfigError):
        rc.run(a, n, check=False, env=env())


def test_cli_exit_codes(tmp_path):
    script = ROOT / "scripts" / "render_config.py"
    args = [sys.executable, str(script), "--astrbot-data", str(tmp_path / "a"), "--napcat-config", str(tmp_path / "n")]
    bad = subprocess.run(args, env={"ONEBOT_ACCESS_TOKEN": "", "PYTHONIOENCODING": "utf-8"},
                         capture_output=True, text=True, encoding="utf-8")
    assert bad.returncode == 2 and "ONEBOT_ACCESS_TOKEN" in bad.stderr
    ok = subprocess.run(args, env={"ONEBOT_ACCESS_TOKEN": TOKEN, "PYTHONIOENCODING": "utf-8"},
                        capture_output=True, text=True, encoding="utf-8")
    assert ok.returncode == 0, ok.stderr
    assert (tmp_path / "a" / "cmd_config.json").exists()
