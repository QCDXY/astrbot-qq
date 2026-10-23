"""qq_guard 群聊自然接话的纯逻辑单元测试（不依赖 AstrBot）。"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "astrbot_plugin_qq_guard"

# 以包的形式加载插件目录（chatter.py 使用相对导入），但不导入依赖 AstrBot 的 main.py
_pkg = types.ModuleType("qq_guard_pkg")
_pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules.setdefault("qq_guard_pkg", _pkg)
chatter = importlib.import_module("qq_guard_pkg.chatter")

G = "801"


class FakeClock:
    def __init__(self) -> None:
        self.t = 10_000.0

    def __call__(self) -> float:
        return self.t


def make(clock=None, **over):
    cfg = {"natural_chat_groups": [G], "natural_chat_min_new_messages": 3, "natural_chat_cooldown_seconds": 60,
           "natural_chat_max_replies_per_hour": 2, "natural_chat_name_triggers": ["小助手", "bot"]}
    cfg.update(over)
    return chatter.Chatter(chatter.ChatterConfig.from_mapping(cfg), clock=clock or FakeClock())


def test_config_defaults_and_parsing():
    c = chatter.ChatterConfig.from_mapping(None)
    assert c.groups == frozenset() and c.min_new_messages == 3 and c.quiet_seconds == 4
    c = chatter.ChatterConfig.from_mapping(
        {"natural_chat_groups": "1, 2", "natural_chat_name_triggers": "小助手，bot,小助手",
         "natural_chat_min_new_messages": "x", "natural_chat_cooldown_seconds": -5}
    )
    assert c.groups == {"1", "2"}
    assert c.name_triggers == ("小助手", "bot"), "保持顺序并去重，第一个是机器人的称呼"
    assert c.min_new_messages == 3 and c.cooldown_seconds == 0


def test_disabled_groups_never_evaluate():
    ch = make()
    seq = 0
    for i in range(5):
        seq = ch.observe("999", "a", f"m{i}")
    assert not ch.enabled_for("999") and not ch.should_evaluate("999", seq)
    assert not make(natural_chat_groups=[]).enabled_for(G)


def test_needs_enough_new_messages():
    ch = make()
    s1 = ch.observe(G, "a", "早")
    assert not ch.should_evaluate(G, s1)
    ch.observe(G, "b", "早啊")
    s3 = ch.observe(G, "c", "今天吃什么")
    assert ch.should_evaluate(G, s3)


def test_only_latest_message_evaluates_after_pause():
    """停顿期间来了新消息，旧消息的判断作废，由最新那条负责（避免打断连续发言）。"""
    ch = make(natural_chat_min_new_messages=1)
    s1 = ch.observe(G, "a", "一")
    s2 = ch.observe(G, "a", "二")
    assert not ch.should_evaluate(G, s1)
    assert ch.should_evaluate(G, s2)


def test_name_trigger_skips_message_threshold():
    ch = make()
    seq = ch.observe(G, "a", "小助手你怎么看")
    assert ch.should_evaluate(G, seq)


def test_cooldown_after_bot_speaks_including_at_replies():
    clock = FakeClock()
    ch = make(clock)
    ch.observe_bot(G, "我刚被 @ 回复了一句")
    seq = 0
    for i in range(3):
        seq = ch.observe(G, "a", f"m{i}")
    assert not ch.should_evaluate(G, seq), "冷却中"
    clock.t += 61
    seq = ch.observe(G, "a", "再来一句")
    assert ch.should_evaluate(G, seq)


def test_begin_resets_pending_and_blocks_concurrent_evaluation():
    ch = make(natural_chat_min_new_messages=2)
    ch.observe(G, "a", "1")
    seq = ch.observe(G, "b", "2")
    assert ch.should_evaluate(G, seq)
    ch.begin(G)
    seq = ch.observe(G, "c", "3")
    assert not ch.should_evaluate(G, seq), "判断进行中不重复判断"
    ch.end(G, replied=False)
    assert not ch.should_evaluate(G, seq), "上一批已评估，需要重新累积"
    seq = ch.observe(G, "d", "4")
    assert ch.should_evaluate(G, seq)


def test_hourly_cap_counts_only_actual_replies():
    clock = FakeClock()
    ch = make(clock, natural_chat_min_new_messages=1, natural_chat_cooldown_seconds=0)
    for _ in range(2):
        seq = ch.observe(G, "a", "x")
        assert ch.should_evaluate(G, seq)
        ch.begin(G)
        ch.end(G, replied=True)
    seq = ch.observe(G, "a", "x")
    ch.begin(G)
    ch.end(G, replied=False)  # 判断为不接话不计数
    seq = ch.observe(G, "a", "x")
    assert not ch.should_evaluate(G, seq), "每小时最多 2 次"
    clock.t += 3601
    seq = ch.observe(G, "a", "x")
    assert ch.should_evaluate(G, seq)


def test_transcript_keeps_recent_messages_with_bot_lines():
    ch = make(natural_chat_context_size=3)
    for i in range(4):
        ch.observe(G, "群友", f"消息{i}")
    ch.observe_bot(G, "我的回复")
    assert ch.transcript(G).splitlines() == ["群友: 消息2", "群友: 消息3", "我: 我的回复"]


def test_context_for_reply_is_messages_since_bot_spoke():
    ch = make(natural_chat_context_size=10)
    ch.observe(G, "a", "旧话题")
    ch.observe_bot(G, "我之前说过")
    ch.observe(G, "b", "新话题一")
    ch.observe(G, "c", "新话题二")
    assert ch.context_for_reply(G) == ["b: 新话题一", "c: 新话题二"]
    other = make()
    other.observe(G, "a", "第一句")
    assert other.context_for_reply(G) == ["a: 第一句"], "机器人还没说过话时使用全部近期消息"


def test_reply_prompt_contains_lines_without_style_instructions():
    p = chatter.build_reply_prompt(["a: 周末去哪", "b: 爬山"])
    assert "a: 周末去哪\nb: 爬山" in p and "自然地接一句话" in p
    assert "简短" not in p and "口语" not in p, "说话风格交给人格设定"


def test_decision_prompt_contains_marker_name_and_transcript():
    p = chatter.build_decision_prompt("小助手", "a: 你好")
    assert chatter.DECISION_MARKER in p and "「小助手」" in p and "a: 你好" in p
    assert "REPLY" in p and "SKIP" in p
    assert "「机器人」" in chatter.build_decision_prompt("", "x")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("REPLY", True), (" reply\n", True), ("**REPLY**", True), ("「REPLY」", True),
     ("SKIP", False), ("", False), (None, False), ("我觉得应该回复", False), ("SKIP. REPLY", False)],
)
def test_parse_decision(text, expected):
    assert chatter.parse_decision(text) is expected
