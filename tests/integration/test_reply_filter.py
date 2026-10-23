"""回复过滤的集成测试：“（不回复）”不发送；机器人主动发起的群聊回复出错时不把错误发到群里，
被 @ 或私聊时照常提示错误。真实 AstrBot + 模拟 NapCat + 模拟 LLM（#silent / #fail 触发对应行为）。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.integration.conftest import BOT_ID, TOKEN, bot_env
from tests.support.astrbot_harness import AstrBotProcess
from tests.support.mock_onebot import MockNapCat

pytestmark = pytest.mark.integration

CHAT_GROUP, PLAIN_GROUP = 901, 902
PLUGIN_CONFIG = {
    "group_allowlist": [],
    "user_rate_limit_count": 0,
    "natural_chat_groups": [str(CHAT_GROUP)],
    "natural_chat_min_new_messages": 1,
    "natural_chat_quiet_seconds": 1,
    "natural_chat_cooldown_seconds": 0,
    "natural_chat_max_replies_per_hour": 50,
}
WAIT = 6


@pytest.fixture(scope="module")
def filter_bot(mock_llm):
    root = Path(tempfile.mkdtemp(prefix="astrbot-filter-"))
    bot = AstrBotProcess(root, bot_env(mock_llm.base_url), plugin_config=PLUGIN_CONFIG)
    bot.prepare()
    bot.start()
    yield bot
    bot.stop()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def napcat(filter_bot):
    nc = MockNapCat(filter_bot.ws_url, TOKEN, self_id=BOT_ID)
    await nc.connect()
    await asyncio.sleep(0.3)
    yield nc
    await nc.close()


async def llm_request_arrives(mock_llm, text: str, wait_seconds: float = 60) -> bool:
    """等待模拟 LLM 收到包含 text 的回复请求（自然接话要先停顿、再判断）。"""
    for _ in range(int(wait_seconds / 0.5)):
        if mock_llm.requests_containing(text):
            return True
        await asyncio.sleep(0.5)
    return False


def sends(nc, *, user=None, group=None):
    out = []
    for m in nc.sent_messages():
        if user is not None and str(m["user_id"]) == str(user) and m["action"] == "send_private_msg":
            out.append(m)
        if group is not None and str(m["group_id"]) == str(group) and m["action"] == "send_group_msg":
            out.append(m)
    return out


async def test_silent_reply_is_not_sent_in_private_chat(napcat, mock_llm):
    await napcat.private_message(91001, "#silent 私聊沉默测试")
    await asyncio.sleep(WAIT)
    assert mock_llm.requests_containing("私聊沉默测试"), "模型确实被调用"
    assert not sends(napcat, user=91001)


async def test_silent_reply_is_not_sent_when_mentioned(napcat):
    await napcat.group_message(PLAIN_GROUP, 91002, "#silent 被@沉默测试", at_bot=True)
    await asyncio.sleep(WAIT)
    assert not sends(napcat, group=PLAIN_GROUP)


async def test_error_is_still_shown_in_private_chat(napcat):
    await napcat.private_message(91003, "#fail 私聊出错测试")
    m = await napcat.wait_for_send(lambda m: str(m["user_id"]) == "91003", timeout=60)
    assert "LLM 响应错误" in m["text"]


async def test_error_is_still_shown_when_mentioned(napcat):
    await napcat.group_message(PLAIN_GROUP, 91004, "#fail 被@出错测试", at_bot=True)
    m = await napcat.wait_for_send(lambda m: str(m["group_id"]) == str(PLAIN_GROUP), timeout=60)
    assert "LLM 响应错误" in m["text"]


async def test_unprompted_reply_error_is_not_posted_to_group(napcat, mock_llm, filter_bot):
    await napcat.group_message(CHAT_GROUP, 91005, "#fail 自然接话出错测试")
    assert await llm_request_arrives(mock_llm, "自然接话出错测试"), "自然接话确实发起了回复请求"
    await asyncio.sleep(WAIT)
    assert not sends(napcat, group=CHAT_GROUP)
    assert "主动发起的回复调用模型失败" in filter_bot.read_log()


async def test_unprompted_silent_reply_is_not_sent(napcat, mock_llm):
    await napcat.group_message(CHAT_GROUP, 91006, "#silent 自然接话沉默测试")
    assert await llm_request_arrives(mock_llm, "自然接话沉默测试")
    await asyncio.sleep(WAIT)
    assert not [m for m in sends(napcat, group=CHAT_GROUP) if "不回复" in m["text"]]


async def test_normal_replies_are_unaffected(napcat):
    await napcat.group_message(PLAIN_GROUP, 91007, "普通的@提问", at_bot=True)
    m = await napcat.wait_for_send(lambda m: str(m["group_id"]) == str(PLAIN_GROUP) and "普通的@提问" in m["text"])
    assert "MOCK_REPLY" in m["text"]


async def test_control_with_filters_disabled_messages_do_get_through(tmp_root, mock_llm):
    """对照组：关闭两项过滤后，“（不回复）”和主动回复的错误都会发出去，证明上面的测试不是碰巧通过。"""
    cfg = dict(PLUGIN_CONFIG, drop_silent_replies=False, hide_unprompted_errors=False)
    bot = AstrBotProcess(tmp_root, bot_env(mock_llm.base_url), plugin_config=cfg)
    bot.prepare()
    bot.start()
    try:
        nc = MockNapCat(bot.ws_url, TOKEN, self_id=BOT_ID)
        await nc.connect()
        try:
            await nc.private_message(91101, "#silent 对照组沉默")
            m = await nc.wait_for_send(lambda m: str(m["user_id"]) == "91101", timeout=60)
            assert "不回复" in m["text"]
            await nc.group_message(CHAT_GROUP, 91102, "#fail 对照组主动回复出错")
            m = await nc.wait_for_send(lambda m: str(m["group_id"]) == str(CHAT_GROUP), timeout=60)
            assert "LLM 响应错误" in m["text"]
        finally:
            await nc.close()
    finally:
        bot.stop()
