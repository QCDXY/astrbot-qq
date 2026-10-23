"""群聊自然接话的集成测试：真实 AstrBot + 本项目配置与插件，模拟 NapCat 与模拟 LLM。

模拟 LLM 对“是否接话”的判断请求：内容含 #skip 时回答 SKIP，否则 REPLY。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.integration.conftest import BOT_ID, TOKEN, bot_env
from tests.support.astrbot_harness import AstrBotProcess
from tests.support.mock_onebot import MockNapCat

pytestmark = pytest.mark.integration

QUIET = 1
NATURAL_GROUPS = ["801", "802", "803", "804", "805", "806"]
OFF_GROUP = 888
PLUGIN_CONFIG = {
    "group_allowlist": [],
    "user_rate_limit_count": 0,
    "natural_chat_groups": NATURAL_GROUPS,
    "natural_chat_name_triggers": ["小助手"],
    "natural_chat_min_new_messages": 2,
    "natural_chat_quiet_seconds": QUIET,
    "natural_chat_cooldown_seconds": 0,
    "natural_chat_max_replies_per_hour": 50,
}
NO_REPLY_WAIT = QUIET + 4


@pytest.fixture(scope="module")
def chat_bot(mock_llm):
    root = Path(tempfile.mkdtemp(prefix="astrbot-chat-"))
    bot = AstrBotProcess(root, bot_env(mock_llm.base_url), plugin_config=PLUGIN_CONFIG)
    bot.prepare()
    bot.start()
    yield bot
    bot.stop()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def napcat(chat_bot):
    nc = MockNapCat(chat_bot.ws_url, TOKEN, self_id=BOT_ID)
    await nc.connect()
    await asyncio.sleep(0.3)
    yield nc
    await nc.close()


def to_group(gid):
    return lambda m: m["action"] == "send_group_msg" and str(m["group_id"]) == str(gid)


def decisions_with(mock_llm, text):
    return [r for r in list(mock_llm.decision_requests) if text in json.dumps(r, ensure_ascii=False)]


def replies_with(mock_llm, text):
    return [r for r in list(mock_llm.requests) if text in json.dumps(r, ensure_ascii=False)]


async def test_joins_conversation_without_mention_using_group_context(napcat, mock_llm):
    await napcat.group_message(801, 30001, "周末大家打算去哪玩")
    await napcat.group_message(801, 30002, "我想去爬山，有人一起吗")
    m = await napcat.wait_for_send(to_group(801), timeout=30)
    # 两条消息几乎同时到达，AstrBot 处理顺序不确定；无论哪条触发判断，回复都必须看到两条
    assert "周末大家打算去哪玩" in m["text"] and "爬山" in m["text"], m["text"]
    (decision,) = decisions_with(mock_llm, "我想去爬山")
    assert "周末大家打算去哪玩" in json.dumps(decision, ensure_ascii=False), "判断时看到了最近的群聊"
    reply_req = replies_with(mock_llm, "我想去爬山")[-1]
    assert json.dumps(reply_req, ensure_ascii=False).count("周末大家打算去哪玩") == 1, "群聊内容不重复注入"

    # 接话之后再被 @：已经回应过的群聊内容不会再次注入
    await napcat.group_message(801, 30012, "那就定周六吧", at_bot=True)
    await napcat.wait_for_send(lambda m: to_group(801)(m) and "定周六" in m["text"], timeout=30)
    at_req = replies_with(mock_llm, "那就定周六吧")[-1]
    assert "<system_reminder>You are in a group chat" not in json.dumps(at_req, ensure_ascii=False)


async def test_stays_silent_when_model_decides_to_skip(napcat, mock_llm):
    await napcat.group_message(802, 30003, "#skip 我们俩私下聊")
    await napcat.group_message(802, 30004, "#skip 好的")
    await asyncio.sleep(NO_REPLY_WAIT)
    assert decisions_with(mock_llm, "#skip 好的"), "做过一次判断"
    assert not [m for m in napcat.sent_messages() if to_group(802)(m)]
    assert not replies_with(mock_llm, "#skip 好的")


async def test_single_message_does_not_trigger_evaluation(napcat, mock_llm):
    await napcat.group_message(803, 30005, "就一句话而已")
    await asyncio.sleep(NO_REPLY_WAIT)
    assert not decisions_with(mock_llm, "就一句话而已")
    assert not [m for m in napcat.sent_messages() if to_group(803)(m)]


async def test_name_trigger_gets_a_reply_without_mention(napcat):
    await napcat.group_message(804, 30006, "小助手你觉得呢")
    m = await napcat.wait_for_send(to_group(804), timeout=30)
    assert "小助手你觉得呢" in m["text"]


async def test_burst_of_messages_is_evaluated_once_after_pause(napcat, mock_llm):
    for i in range(4):
        await napcat.group_message(805, 30007, f"连发第{i}条")
    await napcat.wait_for_send(to_group(805), timeout=30)
    await asyncio.sleep(1)
    assert len(decisions_with(mock_llm, "连发第3条")) == 1
    assert len([m for m in napcat.sent_messages() if to_group(805)(m)]) == 1


async def test_disabled_group_is_never_evaluated(napcat, mock_llm):
    await napcat.group_message(OFF_GROUP, 30008, "没开自然接话的群一")
    await napcat.group_message(OFF_GROUP, 30009, "没开自然接话的群二")
    await asyncio.sleep(NO_REPLY_WAIT)
    assert not decisions_with(mock_llm, "没开自然接话的群")
    assert not [m for m in napcat.sent_messages() if to_group(OFF_GROUP)(m)]


async def test_mention_still_replies_and_sees_earlier_group_messages(napcat, mock_llm):
    await napcat.group_message(806, 30010, "刚才说到那首新歌")
    await napcat.group_message(806, 30011, "她叫什么名字来着", at_bot=True)
    m = await napcat.wait_for_send(to_group(806), timeout=30)
    assert "她叫什么名字来着" in m["text"]
    req = replies_with(mock_llm, "她叫什么名字来着")[-1]
    assert "刚才说到那首新歌" in json.dumps(req, ensure_ascii=False), "@ 回复也带上群聊上下文"
