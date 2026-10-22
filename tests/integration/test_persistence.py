"""重启恢复与模型故障的集成测试（每个测试单独启动 AstrBot 进程）。"""

from __future__ import annotations

import json

import pytest

from tests.integration.conftest import BOT_ID, PLUGIN_CONFIG, TOKEN, bot_env
from tests.support.astrbot_harness import AstrBotProcess
from tests.support.mock_onebot import MockNapCat

pytestmark = pytest.mark.integration


def to_user(uid):
    return lambda m: m["action"] == "send_private_msg" and str(m["user_id"]) == str(uid)


async def _chat(bot: AstrBotProcess, uid: int, text: str, predicate=None) -> dict:
    nc = MockNapCat(bot.ws_url, TOKEN, self_id=BOT_ID)
    await nc.connect()
    try:
        await nc.private_message(uid, text)
        return await nc.wait_for_send(predicate or to_user(uid), timeout=60)
    finally:
        await nc.close()


async def test_restart_keeps_config_history_and_plugin_settings(tmp_root, mock_llm):
    bot = AstrBotProcess(tmp_root, bot_env(mock_llm.base_url), plugin_config=PLUGIN_CONFIG)
    bot.prepare()
    bot.start()
    try:
        await _chat(bot, 50001, "请记住口令：紫藤")
        bot.wait_for_history("紫藤")
    finally:
        bot.stop()

    # 模拟用户在 WebUI 中修改的配置（非连接字段），重启前 init 再次运行不应覆盖它
    cfg_path = bot.data / "cmd_config.json"
    conf = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    password_hash = conf["dashboard"]["pbkdf2_password"]
    assert password_hash, "首次启动后应已生成 WebUI 密码哈希"
    conf["platform_settings"]["reply_prefix"] = "[持久化]"
    cfg_path.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")

    bot.prepare()  # 与 compose 中 init 服务每次启动前运行一致
    conf_after = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    assert conf_after["platform_settings"]["reply_prefix"] == "[持久化]"
    assert conf_after["dashboard"]["pbkdf2_password"] == password_hash

    bot.start()
    try:
        m = await _chat(bot, 50001, "口令是什么", lambda m: to_user(50001)(m) and "口令是什么" in m["text"])
        assert m["text"].startswith("[持久化]"), m["text"]
        req = mock_llm.requests_containing("口令是什么")[-1]
        assert any("紫藤" in u for u in mock_llm.user_messages(req)), "重启后会话历史应保留"
        assert "[qq_guard] 已加载：群白名单 1 个" in bot.read_log()
    finally:
        bot.stop()


async def test_invalid_llm_key_does_not_fake_success(tmp_root, mock_llm):
    bot = AstrBotProcess(tmp_root, bot_env(mock_llm.base_url, key="wrong-key"), plugin_config={})
    bot.prepare()
    bot.start()
    try:
        nc = MockNapCat(bot.ws_url, TOKEN, self_id=BOT_ID)
        await nc.connect()
        try:
            await nc.private_message(60001, "密钥错误时的提问")
            m = await nc.wait_for_send(to_user(60001), timeout=60)
        finally:
            await nc.close()
        assert "MOCK_REPLY" not in m["text"]
        assert not mock_llm.requests_containing("密钥错误时的提问")
        assert "Bearer wrong-key" in mock_llm.auth_headers
    finally:
        bot.stop()


async def test_starts_without_initial_password_and_generates_one(tmp_root, mock_llm):
    """ASTRBOT_DASHBOARD_INITIAL_PASSWORD 留空（.env 默认）时 AstrBot 应正常启动并随机生成密码。"""
    env = bot_env(mock_llm.base_url)
    env["ASTRBOT_DASHBOARD_INITIAL_PASSWORD"] = ""
    bot = AstrBotProcess(tmp_root, env, plugin_config={})
    bot.prepare()
    bot.start()
    try:
        m = await _chat(bot, 70001, "无初始密码启动")
        assert "MOCK_REPLY" in m["text"]
        dashboard = json.loads((bot.data / "cmd_config.json").read_text(encoding="utf-8-sig"))["dashboard"]
        assert dashboard["pbkdf2_password"]
        assert dashboard["password_change_required"] is True
        assert "Initial password" in bot.read_log()
    finally:
        bot.stop()
