"""qq_guard 插件纯逻辑单元测试（不依赖 AstrBot）。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "astrbot_plugin_qq_guard"
_spec = importlib.util.spec_from_file_location("qq_guard_logic", PLUGIN_DIR / "guard.py")
guard = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = guard
_spec.loader.exec_module(guard)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def msg(sender="1", self_id="10001", group="", admin=False):
    return guard.MessageInfo(sender_id=sender, self_id=self_id, group_id=group, is_admin=admin)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, set()),
        ([], set()),
        ([123, " 456 ", "", "123"], {"123", "456"}),
        ("111, 222，333  444", {"111", "222", "333", "444"}),
    ],
)
def test_parse_id_list(raw, expected):
    assert guard.parse_id_list(raw) == frozenset(expected)


def test_sliding_window_limiter():
    clock = FakeClock()
    lim = guard.SlidingWindowLimiter(2, 10, clock=clock)
    assert lim.hit("a") and lim.hit("a")
    assert not lim.hit("a")
    assert lim.hit("b"), "不同 key 独立计数"
    assert lim.remaining("a") == 0
    assert 0 < lim.retry_after("a") <= 10
    clock.t += 10
    assert lim.remaining("a") == 2
    assert lim.hit("a")


def test_limiter_disabled():
    lim = guard.SlidingWindowLimiter(0, 60)
    assert all(lim.hit("x") for _ in range(100))
    assert lim.remaining("x") is None


def test_limiter_gc_bounds_memory():
    clock = FakeClock()
    lim = guard.SlidingWindowLimiter(1, 5, clock=clock, max_keys=10)
    for i in range(50):
        lim.hit(str(i))
        clock.t += 6
    assert len(lim._hits) <= 11


def test_config_from_mapping_defaults_and_bad_values():
    c = guard.GuardConfig.from_mapping(None)
    assert c.group_allowlist == frozenset() and c.user_rate_limit_count == 0
    c = guard.GuardConfig.from_mapping({"group_allowlist": ["1"], "user_rate_limit_count": "x"})
    assert c.group_allowlist == {"1"} and c.user_rate_limit_count == 0


def test_self_message_is_blocked_even_for_admin():
    g = guard.Guard(guard.GuardConfig())
    assert g.check(msg(sender="10001", admin=True)) is guard.Verdict.SELF_MESSAGE


def test_group_allowlist_only_affects_groups():
    g = guard.Guard(guard.GuardConfig(group_allowlist=frozenset({"777"})))
    assert g.check(msg(group="777")) is guard.Verdict.ALLOW
    assert g.check(msg(group="888")) is guard.Verdict.GROUP_NOT_ALLOWED
    assert g.check(msg(group="")) is guard.Verdict.ALLOW, "私聊不受群白名单影响"
    assert g.check(msg(group="888", admin=True)) is guard.Verdict.ALLOW, "管理员不受限"


def test_empty_allowlist_allows_all_groups():
    g = guard.Guard(guard.GuardConfig())
    assert g.check(msg(group="anything")) is guard.Verdict.ALLOW


def test_rate_limit_per_user_and_notify_once():
    clock = FakeClock()
    g = guard.Guard(guard.GuardConfig(user_rate_limit_count=2, user_rate_limit_window=60), clock=clock)
    assert g.check(msg("u1")) is guard.Verdict.ALLOW
    assert g.check(msg("u1")) is guard.Verdict.ALLOW
    assert g.check(msg("u1")) is guard.Verdict.RATE_LIMITED
    assert g.should_notify("u1") is True
    assert g.should_notify("u1") is False
    assert g.check(msg("u2")) is guard.Verdict.ALLOW
    assert g.check(msg("u1", admin=True)) is guard.Verdict.ALLOW
    clock.t += 61
    assert g.check(msg("u1")) is guard.Verdict.ALLOW
    assert g.should_notify("u1") is True


def test_blocked_group_does_not_consume_quota():
    g = guard.Guard(guard.GuardConfig(frozenset({"1"}), user_rate_limit_count=1))
    assert g.check(msg("u", group="2")) is guard.Verdict.GROUP_NOT_ALLOWED
    assert g.check(msg("u", group="1")) is guard.Verdict.ALLOW


def test_plugin_metadata_and_schema_are_valid():
    meta = yaml.safe_load((PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    for key in ("name", "desc", "version", "author"):
        assert meta.get(key), key
    assert meta["name"] == PLUGIN_DIR.name
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    cfg = guard.GuardConfig.from_mapping({k: v.get("default") for k, v in schema.items()})
    assert {"group_allowlist", "user_rate_limit_count", "user_rate_limit_window", "notify_on_limit"} <= set(schema)
    assert all(k.startswith(("natural_chat_", "anthropic_")) for k in set(schema) - {
        "group_allowlist", "user_rate_limit_count", "user_rate_limit_window", "notify_on_limit", "blocked_llm_tools",
        "drop_silent_replies", "hide_unprompted_errors"})
    assert schema["drop_silent_replies"]["default"] is True and schema["hide_unprompted_errors"]["default"] is True
    assert set(schema["blocked_llm_tools"]["default"]) == guard.DEFAULT_BLOCKED_LLM_TOOLS
    assert cfg.user_rate_limit_count > 0 and cfg.user_rate_limit_window > 0
    assert schema["anthropic_history_cache"]["default"] is True
    assert schema["anthropic_cache_ttl"]["default"] in schema["anthropic_cache_ttl"]["options"]
    for item in schema.values():
        assert item["type"] in {"string", "int", "float", "bool", "list", "object", "text"}


class FakeToolSet:
    def __init__(self, names):
        self._names = list(names)

    def names(self):
        return list(self._names)

    def remove_tool(self, name):
        self._names.remove(name)


def test_default_blocked_tools_and_explicit_empty_list():
    assert guard.GuardConfig.from_mapping({}).blocked_llm_tools == guard.DEFAULT_BLOCKED_LLM_TOOLS
    assert guard.GuardConfig.from_mapping({"blocked_llm_tools": []}).blocked_llm_tools == frozenset()
    assert guard.GuardConfig.from_mapping({"blocked_llm_tools": ["x"]}).blocked_llm_tools == {"x"}


def test_strip_blocked_tools_keeps_web_search():
    ts = FakeToolSet(["web_search_deepseek", "deepseek_fetch_web_page", "send_message_to_user", "future_task"])
    removed = guard.strip_blocked_tools(ts, guard.DEFAULT_BLOCKED_LLM_TOOLS)
    assert sorted(removed) == ["future_task", "send_message_to_user"]
    assert ts.names() == ["web_search_deepseek", "deepseek_fetch_web_page"]
    assert guard.strip_blocked_tools(None, guard.DEFAULT_BLOCKED_LLM_TOOLS) == []


@pytest.mark.parametrize(
    "text",
    ["（不回复）", "(不回复)", " 【不回复】 ", "不回复", "（不回复）。", "「不回复」\n"],
)
def test_silent_markers_are_recognized(text):
    assert guard.is_silent_reply(text)


@pytest.mark.parametrize(
    "text",
    ["", None, "我先不回复了，你们聊", "（不回复，继续潜水）", "不回复你", "回复"],
)
def test_real_replies_are_not_silent(text):
    assert not guard.is_silent_reply(text)


def test_llm_error_reply_detection():
    assert guard.is_llm_error_reply("LLM 响应错误: All chat models failed: EmptyModelOutputError: x")
    assert guard.is_llm_error_reply("  LLM 响应错误: 未知错误")
    assert not guard.is_llm_error_reply("模型说：LLM 响应错误是什么意思？")
    assert not guard.is_llm_error_reply("")


def test_should_drop_reply_policy():
    cfg = guard.GuardConfig()
    err = "LLM 响应错误: All chat models failed"
    assert guard.should_drop_reply("（不回复）", unprompted=False, config=cfg) == "silent"
    assert guard.should_drop_reply("（不回复）", unprompted=True, config=cfg) == "silent"
    assert guard.should_drop_reply(err, unprompted=True, config=cfg) == "error"
    assert guard.should_drop_reply(err, unprompted=False, config=cfg) is None, "被 @ / 私聊时错误照常提示"
    assert guard.should_drop_reply("正常回复", unprompted=True, config=cfg) is None
    off = guard.GuardConfig.from_mapping({"drop_silent_replies": False, "hide_unprompted_errors": False})
    assert guard.should_drop_reply("（不回复）", unprompted=True, config=off) is None
    assert guard.should_drop_reply(err, unprompted=True, config=off) is None
