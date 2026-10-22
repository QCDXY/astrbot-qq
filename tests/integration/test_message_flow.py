"""关键消息处理链路的集成测试。

真实组件：AstrBot 4.29.1（与镜像同版本）、本项目生成的配置、qq_guard 插件。
模拟组件：NapCat（OneBot v11 反向 WS 客户端）、OpenAI 兼容 LLM。
因此这里验证的是 “NapCat -> AstrBot -> LLM -> AstrBot -> NapCat” 中 AstrBot 侧的全部逻辑；
真实 QQ 收发与真实模型调用需要人工联调。
"""

from __future__ import annotations

import asyncio

import pytest
import websockets

from tests.integration.conftest import (
    ADMIN_ID,
    ALLOWED_GROUP,
    BOT_ID,
    OTHER_GROUP,
    TOKEN,
    USER_LIMIT,
)
from tests.support.mock_onebot import MockNapCat

pytestmark = pytest.mark.integration

QUIET_PERIOD = 4.0  # 断言“没有回复”时等待的秒数


@pytest.fixture
async def napcat(astrbot):
    nc = MockNapCat(astrbot.ws_url, TOKEN, self_id=BOT_ID)
    await nc.connect()
    await asyncio.sleep(0.3)
    yield nc
    await nc.close()


def to_user(uid):
    return lambda m: m["action"] == "send_private_msg" and str(m["user_id"]) == str(uid)


def to_group(gid):
    return lambda m: m["action"] == "send_group_msg" and str(m["group_id"]) == str(gid)


async def test_private_message_gets_llm_reply(napcat, mock_llm):
    await napcat.private_message(20001, "你好，私聊测试")
    m = await napcat.wait_for_send(to_user(20001))
    assert "MOCK_REPLY" in m["text"] and "你好，私聊测试" in m["text"]
    req = mock_llm.requests_containing("你好，私聊测试")[-1]
    assert req["model"] == "mock-model"
    # API Key 来自环境变量引用 "$LLM_API_KEY"，而非写在配置文件里
    assert "Bearer test-llm-key" in mock_llm.auth_headers


async def test_group_message_without_mention_is_ignored(napcat, mock_llm):
    before = len(mock_llm.requests)
    await napcat.group_message(ALLOWED_GROUP, 30001, "大家好，这句没有@机器人")
    await asyncio.sleep(QUIET_PERIOD)
    assert not [m for m in napcat.sent_messages() if to_group(ALLOWED_GROUP)(m)]
    assert len(mock_llm.requests) == before


async def test_group_message_with_mention_gets_reply(napcat):
    await napcat.group_message(ALLOWED_GROUP, 30002, "群聊@测试", at_bot=True)
    m = await napcat.wait_for_send(to_group(ALLOWED_GROUP))
    assert "群聊@测试" in m["text"]


async def test_context_is_isolated_per_session(napcat, mock_llm):
    await napcat.private_message(20002, "我的暗号是蓝鲸")
    await napcat.wait_for_send(lambda m: to_user(20002)(m) and "蓝鲸" in m["text"])
    await napcat.private_message(20002, "我的暗号是什么")
    await napcat.wait_for_send(lambda m: to_user(20002)(m) and "暗号是什么" in m["text"])
    same_session = mock_llm.requests_containing("我的暗号是什么")[-1]
    assert any("蓝鲸" in u for u in mock_llm.user_messages(same_session)), "同一会话应带上历史上下文"

    await napcat.private_message(20003, "另一个人问：暗号是什么")
    await napcat.wait_for_send(to_user(20003))
    other = mock_llm.requests_containing("另一个人问")[-1]
    assert not any("蓝鲸" in u for u in mock_llm.user_messages(other)), "不同会话不能共享上下文"


async def test_help_command_without_llm(napcat, mock_llm):
    before = len(mock_llm.requests)
    await napcat.private_message(20004, "/help")
    m = await napcat.wait_for_send(to_user(20004))
    assert "/sid" in m["text"] or "help" in m["text"].lower()
    assert len(mock_llm.requests) == before


async def test_sid_command(napcat):
    await napcat.private_message(20005, "/sid")
    m = await napcat.wait_for_send(to_user(20005))
    assert "20005" in m["text"]


async def test_guard_status_command_in_group(napcat):
    await napcat.group_message(ALLOWED_GROUP, 30003, "/guard")
    m = await napcat.wait_for_send(lambda m: to_group(ALLOWED_GROUP)(m) and "qq_guard" in m["text"])
    assert f"群号: {ALLOWED_GROUP}" in m["text"]
    assert "本群允许: 是" in m["text"]


async def test_group_not_in_allowlist_is_ignored(napcat, mock_llm):
    before = len(mock_llm.requests)
    await napcat.group_message(OTHER_GROUP, 30004, "白名单外的群", at_bot=True)
    await asyncio.sleep(QUIET_PERIOD)
    assert not [m for m in napcat.sent_messages() if to_group(OTHER_GROUP)(m)]
    assert len(mock_llm.requests) == before


async def test_admin_bypasses_group_allowlist(napcat):
    await napcat.group_message(OTHER_GROUP, ADMIN_ID, "管理员在白名单外的群", at_bot=True)
    m = await napcat.wait_for_send(to_group(OTHER_GROUP))
    assert "管理员在白名单外的群" in m["text"]


async def test_per_user_rate_limit(napcat, mock_llm):
    uid = 40001
    for i in range(USER_LIMIT):
        await napcat.private_message(uid, f"限流测试{i}")
        await napcat.wait_for_send(lambda m, i=i: to_user(uid)(m) and f"限流测试{i}" in m["text"])
    before = len(mock_llm.requests)
    await napcat.private_message(uid, "限流测试-超出")
    m = await napcat.wait_for_send(lambda m: to_user(uid)(m) and "太频繁" in m["text"])
    assert "秒后再试" in m["text"]
    # 再次超出不会重复提示，也不会调用 LLM
    await napcat.private_message(uid, "限流测试-再次超出")
    await asyncio.sleep(QUIET_PERIOD)
    assert len([m for m in napcat.sent_messages() if to_user(uid)(m) and "太频繁" in m["text"]]) == 1
    assert len(mock_llm.requests) == before
    # 其他用户不受影响
    await napcat.private_message(40002, "别人不受影响")
    await napcat.wait_for_send(to_user(40002))


async def test_bot_does_not_reply_to_itself(napcat, mock_llm):
    before = len(mock_llm.requests)
    await napcat.group_message(ALLOWED_GROUP, BOT_ID, "机器人自己的消息", at_bot=True)
    await napcat.private_message(BOT_ID, "机器人自己的私聊")
    await asyncio.sleep(QUIET_PERIOD)
    assert not [m for m in napcat.sent_messages() if "机器人自己" in m["text"]]
    assert len(mock_llm.requests) == before


@pytest.mark.parametrize(("token", "status"), [("wrong-token-000000000000", 403), ("", 401)])
async def test_bad_access_token_is_rejected(astrbot, token, status):
    bad = MockNapCat(astrbot.ws_url, token, self_id=BOT_ID)
    with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
        await bad.connect()
    assert exc.value.response.status_code == status
    await bad.close()


async def test_reconnect_after_disconnect(astrbot):
    first = MockNapCat(astrbot.ws_url, TOKEN, self_id=BOT_ID)
    await first.connect()
    await first.private_message(20006, "断线前")
    await first.wait_for_send(to_user(20006))
    await first.close()  # 模拟 NapCat 断线

    await asyncio.sleep(1)
    second = MockNapCat(astrbot.ws_url, TOKEN, self_id=BOT_ID)
    await second.connect()  # 模拟 NapCat 按 reconnectInterval 重连
    try:
        await second.private_message(20006, "重连后")
        m = await second.wait_for_send(lambda m: to_user(20006)(m) and "重连后" in m["text"])
        assert "MOCK_REPLY" in m["text"]
    finally:
        await second.close()


async def test_dashboard_is_served(astrbot):
    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{astrbot.dashboard_port}/") as r:
            assert r.status == 200
