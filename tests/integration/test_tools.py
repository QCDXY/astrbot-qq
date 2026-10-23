"""联网搜索与工具屏蔽的集成测试：检查真实 AstrBot 实际发给模型的工具列表。

不会调用真实的搜索服务（模拟 LLM 不发起工具调用），真实搜索需人工联调。
"""

from __future__ import annotations

import json

import pytest

from tests.integration.conftest import BOT_ID, TOKEN, bot_env
from tests.support.astrbot_harness import AstrBotProcess
from tests.support.mock_onebot import MockNapCat

pytestmark = pytest.mark.integration

SEARCH_TOOLS = {"web_search_deepseek", "deepseek_fetch_web_page"}


def tool_names(request: dict) -> set[str]:
    return {t.get("function", {}).get("name") for t in request.get("tools", [])}


async def test_web_search_tools_offered_and_risky_builtins_blocked(tmp_root, mock_llm):
    bot = AstrBotProcess(tmp_root, bot_env(mock_llm.base_url), plugin_config={})
    bot.prepare()
    cfg_path = bot.data / "cmd_config.json"
    conf = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    assert "tool_use" in conf["provider"][0]["modalities"], "种子配置应允许工具调用"
    conf["provider_settings"].update(
        {
            "web_search": True,
            "websearch_provider": "deepseek",
            "websearch_deepseek_key": ["fake-search-key"],
            # 即使有人在配置里重新打开定时任务工具，插件也会把它屏蔽
            "proactive_capability": {"add_cron_tools": True},
        }
    )
    cfg_path.write_text(json.dumps(conf, ensure_ascii=False), encoding="utf-8")
    bot.start()
    try:
        nc = MockNapCat(bot.ws_url, TOKEN, self_id=BOT_ID)
        await nc.connect()
        try:
            await nc.private_message(80001, "联网搜索工具测试")
            await nc.wait_for_send(lambda m: str(m["user_id"]) == "80001", timeout=60)
        finally:
            await nc.close()
        req = mock_llm.requests_containing("联网搜索工具测试")[-1]
        names = tool_names(req)
        assert SEARCH_TOOLS <= names, names
        assert not names & {"send_message_to_user", "future_task"}, names
        log = bot.read_log()
        assert "屏蔽工具 future_task,send_message_to_user" in log
        # 插件在真实 AstrBot 中成功挂上 Claude 对话历史缓存（未变化的 Anthropic 适配器）
        assert "Claude 对话历史缓存：开启（TTL 5m）" in log, log[-2000:]
    finally:
        bot.stop()
