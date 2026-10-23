"""Claude 对话历史缓存断点的测试。

纯逻辑部分不依赖 AstrBot；端到端部分用真实的 AstrBot ProviderAnthropic（pip 包，与镜像同版本）
向本地模拟的 /v1/messages 发请求，检查实际发出的 JSON。
"""

from __future__ import annotations

import copy
import importlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "astrbot_plugin_qq_guard"

_pkg = types.ModuleType("qq_guard_pkg")
_pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules.setdefault("qq_guard_pkg", _pkg)
pc = importlib.import_module("qq_guard_pkg.prompt_cache")

EPHEMERAL = {"type": "ephemeral"}


def markers(payload: dict) -> list[tuple[str, dict]]:
    """按出现顺序列出 (位置, cache_control)。"""
    out = []
    for i, b in enumerate(payload.get("tools") or []):
        if "cache_control" in b:
            out.append((f"tools[{i}]", b["cache_control"]))
    for i, b in enumerate(payload.get("system") or []):
        if "cache_control" in b:
            out.append((f"system[{i}]", b["cache_control"]))
    for i, m in enumerate(payload.get("messages") or []):
        if isinstance(m.get("content"), list):
            for j, b in enumerate(m["content"]):
                if isinstance(b, dict) and "cache_control" in b:
                    out.append((f"messages[{i}][{j}]", b["cache_control"]))
    return out


def strip_markers(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_markers(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [strip_markers(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# 纯逻辑
# --------------------------------------------------------------------------- #


def base_payload() -> dict:
    return {
        "system": [{"type": "text", "text": "persona", "cache_control": dict(EPHEMERAL)}],
        "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": [{"type": "text", "text": "你好呀"}]},
            {
                "role": "user",
                "content": [{"type": "text", "text": "今天吃什么"}, {"type": "text", "text": ""}],
            },
        ],
    }


def test_marks_last_non_empty_block_without_touching_original():
    payload = base_payload()
    original_messages = payload["messages"]
    snapshot = copy.deepcopy(original_messages)

    assert pc.add_history_breakpoint(payload) is True
    assert markers(payload) == [("system[0]", EPHEMERAL), ("messages[2][0]", EPHEMERAL)]
    # 字符串内容统一成单个 text 块
    assert payload["messages"][0]["content"] == [{"type": "text", "text": "你好"}]
    # 调用方的对象（AstrBot 会话历史）不被修改
    assert original_messages == snapshot


def test_skips_when_messages_already_marked_or_slots_full():
    payload = base_payload()
    payload["messages"][1]["content"][0]["cache_control"] = dict(EPHEMERAL)
    assert pc.add_history_breakpoint(payload) is False

    payload = base_payload()
    payload["tools"] = [{"name": f"t{i}", "cache_control": dict(EPHEMERAL)} for i in range(3)]
    assert pc.add_history_breakpoint(payload) is False
    assert len(markers(payload)) == 4


def test_skip_context_and_unmarkable_last_message():
    payload = base_payload()
    with pc.skip_history_cache():
        assert pc.add_history_breakpoint(payload) is False
    assert pc.add_history_breakpoint(payload) is True

    empty = {"messages": [{"role": "user", "content": [{"type": "text", "text": "  "}]}]}
    assert pc.add_history_breakpoint(empty) is False
    assert pc.add_history_breakpoint({"messages": []}) is False


def test_one_hour_ttl_upgrades_earlier_breakpoints():
    payload = base_payload()
    payload["tools"] = [{"name": "a"}, {"name": "b", "cache_control": dict(EPHEMERAL)}]
    assert pc.add_history_breakpoint(payload, "1h") is True
    one_hour = {"type": "ephemeral", "ttl": "1h"}
    assert markers(payload) == [("tools[1]", one_hour), ("system[0]", one_hour), ("messages[2][0]", one_hour)]


@pytest.mark.parametrize(("raw", "ttl"), [(None, "5m"), ("1H", "1h"), (" 5m ", "5m"), ("2h", "5m")])
def test_normalize_ttl(raw, ttl):
    assert pc.normalize_ttl(raw) == ttl


def test_install_is_idempotent_and_reversible():
    class Provider:
        @classmethod
        def _apply_explicit_prompt_cache_breakpoints(cls, payloads):
            payloads["system"][-1]["cache_control"] = dict(EPHEMERAL)

    original = Provider.__dict__["_apply_explicit_prompt_cache_breakpoints"]
    assert pc.install(Provider) and pc.install(Provider, "1h")
    payload = {"system": [{"type": "text", "text": "s"}], "messages": [{"role": "user", "content": "hi"}]}
    Provider._apply_explicit_prompt_cache_breakpoints(payload)
    assert [c.get("ttl") for _, c in markers(payload)] == ["1h", "1h"]

    pc.uninstall(Provider)
    assert Provider.__dict__["_apply_explicit_prompt_cache_breakpoints"] is original
    assert pc.install(type("NoHook", (), {})) is False


# --------------------------------------------------------------------------- #
# 端到端：真实 ProviderAnthropic + 模拟 Anthropic Messages API
# --------------------------------------------------------------------------- #


@pytest.fixture
async def mock_anthropic():
    bodies: list[dict] = []

    async def messages(request: web.Request) -> web.Response:
        body = await request.json()
        bodies.append(body)
        return web.json_response(
            {
                "id": f"msg_{len(bodies)}",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [{"type": "text", "text": f"reply {len(bodies)}"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 0},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", bodies
    await runner.cleanup()


@pytest.fixture
def anthropic_provider():
    pytest.importorskip("anthropic")
    from astrbot.core.provider.sources.anthropic_source import ProviderAnthropic

    def make(base_url: str):
        cfg = {
            "id": "claude",
            "type": "anthropic_chat_completion",
            "key": ["test-key"],
            "api_base": base_url,
            "model": "claude-haiku-5-5",
            "timeout": 10,
        }
        return ProviderAnthropic(cfg, {})

    yield ProviderAnthropic, make
    pc.uninstall(ProviderAnthropic)


async def test_real_provider_caches_history_across_turns(mock_anthropic, anthropic_provider):
    base_url, bodies = mock_anthropic
    cls, make = anthropic_provider
    assert pc.install(cls)
    provider = make(base_url)
    system = "你是群聊机器人小助手。" * 20

    history: list[dict] = []
    for text in ("第一句", "第二句", "第三句"):
        snapshot = copy.deepcopy(history)
        resp = await provider.text_chat(prompt=text, contexts=history, system_prompt=system)
        assert history == snapshot, "缓存断点不应写进会话历史"
        history += [{"role": "user", "content": text}, {"role": "assistant", "content": resp.completion_text}]

    for body in bodies:
        assert [where for where, _ in markers(body)] == [
            "system[0]",
            f"messages[{len(body['messages']) - 1}][0]",
        ]
    # 每一轮的请求都以上一轮的完整请求为前缀（去掉断点标记后逐字节一致），因此能读到上一轮写入的缓存
    for prev, cur in zip(bodies, bodies[1:], strict=False):
        assert strip_markers(cur["system"]) == strip_markers(prev["system"])
        assert strip_markers(cur["messages"][: len(prev["messages"])]) == strip_markers(prev["messages"])


async def test_real_provider_without_patch_only_caches_system(mock_anthropic, anthropic_provider):
    base_url, bodies = mock_anthropic
    _, make = anthropic_provider
    await make(base_url).text_chat(prompt="hi", system_prompt="persona")
    assert [where for where, _ in markers(bodies[0])] == ["system[0]"]


async def test_real_provider_skip_context(mock_anthropic, anthropic_provider):
    base_url, bodies = mock_anthropic
    cls, make = anthropic_provider
    assert pc.install(cls, "1h")
    provider = make(base_url)
    with pc.skip_history_cache():
        await provider.text_chat(prompt="判断", system_prompt="judge")
    await provider.text_chat(prompt="hi", system_prompt="persona")
    assert markers(bodies[0]) == [("system[0]", EPHEMERAL)]
    one_hour = {"type": "ephemeral", "ttl": "1h"}
    assert markers(bodies[1]) == [("system[0]", one_hour), ("messages[0][0]", one_hour)]
