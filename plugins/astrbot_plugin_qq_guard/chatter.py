"""群聊自然接话的纯逻辑部分，不依赖 AstrBot，便于单元测试。

思路：不在每条消息上都调用模型。只有当
  1. 该群启用了自然接话；
  2. 群聊停顿了一会儿（quiet_seconds 内没有新消息，避免插话打断）；
  3. 距离机器人上次发言已过冷却时间；
  4. 自上次判断以来累积了足够多的新消息（或有人提到了机器人的名字）；
  5. 本小时主动发言次数未超上限；
时，才用一次很短的模型调用判断“现在有没有自然的接话点”，判断为是才真正回复。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .guard import parse_id_list

DECISION_MARKER = "【群聊接话判断】"

DECISION_SYSTEM_PROMPT = (
    "你是一个判断器，只负责判断群聊里的一个成员此刻是否应该自然地接话。"
    "只输出一个词：REPLY 或 SKIP，不要输出其他任何内容。"
)


@dataclass
class ChatterConfig:
    groups: frozenset[str] = field(default_factory=frozenset)
    min_new_messages: int = 3
    cooldown_seconds: float = 60
    quiet_seconds: float = 4
    max_replies_per_hour: int = 20
    name_triggers: tuple[str, ...] = ()
    context_size: int = 20

    @classmethod
    def from_mapping(cls, m: dict | None) -> ChatterConfig:
        m = m or {}
        return cls(
            groups=parse_id_list(m.get("natural_chat_groups")),
            min_new_messages=max(1, _as_int(m.get("natural_chat_min_new_messages"), 3)),
            cooldown_seconds=max(0, _as_int(m.get("natural_chat_cooldown_seconds"), 60)),
            quiet_seconds=max(0, _as_int(m.get("natural_chat_quiet_seconds"), 4)),
            max_replies_per_hour=max(0, _as_int(m.get("natural_chat_max_replies_per_hour"), 20)),
            name_triggers=_ordered_names(m.get("natural_chat_name_triggers")),
            context_size=max(1, _as_int(m.get("natural_chat_context_size"), 20)),
        )


def _ordered_names(raw: object) -> tuple[str, ...]:
    """保持配置中的顺序（第一个视为机器人的称呼），去重去空。"""
    items = raw.replace("，", ",").split(",") if isinstance(raw, str) else list(raw or [])
    out: list[str] = []
    for i in items:
        name = str(i).strip()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def _as_int(v: object, default: int) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass
class _GroupState:
    seq: int = 0
    pending: int = 0
    name_mentioned: bool = False
    busy: bool = False
    last_bot_at: float | None = None
    recent: deque[str] = field(default_factory=deque)
    reply_times: deque[float] = field(default_factory=deque)


class Chatter:
    def __init__(self, config: ChatterConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self._clock = clock
        self._groups: dict[str, _GroupState] = {}

    def enabled_for(self, group_id: str) -> bool:
        return bool(group_id) and group_id in self.config.groups

    def _state(self, group_id: str) -> _GroupState:
        return self._groups.setdefault(group_id, _GroupState())

    def _remember(self, st: _GroupState, line: str) -> None:
        st.recent.append(line)
        while len(st.recent) > self.config.context_size:
            st.recent.popleft()

    def observe(self, group_id: str, sender: str, text: str) -> int:
        """记录一条群成员消息，返回它的序号（用于判断之后是否有更新的消息）。"""
        st = self._state(group_id)
        st.seq += 1
        st.pending += 1
        if any(name and name in text for name in self.config.name_triggers):
            st.name_mentioned = True
        self._remember(st, f"{sender}: {text}")
        return st.seq

    def observe_bot(self, group_id: str, text: str) -> None:
        """记录机器人自己在群里的发言（包括被 @ 时的回复），并重新开始冷却。"""
        st = self._state(group_id)
        st.last_bot_at = self._clock()
        st.pending = 0
        st.name_mentioned = False
        self._remember(st, f"我: {text}")

    def should_evaluate(self, group_id: str, seq: int) -> bool:
        """停顿结束后调用：seq 必须仍是最新消息，且满足冷却、条数与每小时上限。"""
        if not self.enabled_for(group_id):
            return False
        st = self._state(group_id)
        if st.busy or seq != st.seq:
            return False
        now = self._clock()
        if st.last_bot_at is not None and now - st.last_bot_at < self.config.cooldown_seconds:
            return False
        if st.pending < self.config.min_new_messages and not st.name_mentioned:
            return False
        while st.reply_times and now - st.reply_times[0] >= 3600:
            st.reply_times.popleft()
        if len(st.reply_times) >= self.config.max_replies_per_hour:
            return False
        return True

    def begin(self, group_id: str) -> None:
        st = self._state(group_id)
        st.busy = True
        # 无论判断结果如何，这批消息都已评估过，避免下一条消息立刻再次判断
        st.pending = 0
        st.name_mentioned = False

    def end(self, group_id: str, replied: bool) -> None:
        st = self._state(group_id)
        st.busy = False
        if replied:
            st.reply_times.append(self._clock())

    def transcript(self, group_id: str) -> str:
        return "\n".join(self._state(group_id).recent)

    def context_for_reply(self, group_id: str) -> list[str]:
        """自然接话时作为回复提示的群聊内容：机器人上次发言之后的消息（至多 context_size 条）。"""
        lines = list(self._state(group_id).recent)
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("我: "):
                return lines[i + 1 :]
        return lines


def build_decision_prompt(bot_name: str, transcript: str) -> str:
    name = bot_name or "机器人"
    return (
        f"{DECISION_MARKER}\n"
        f"你在群里的昵称是「{name}」，下面是最近的群聊记录（“我”是你自己之前的发言）：\n"
        f"---\n{transcript}\n---\n"
        "判断你现在是否应该自然地接一句话。像一个普通群友那样：\n"
        "- 话题你能自然参与、有人在问问题或求助、有人提到你、或者气氛适合插一句，回答 REPLY；\n"
        "- 别人在私下聊天、话题与你无关、刚说完一段没必要回应、或者内容只是表情/图片/刷屏，回答 SKIP；\n"
        "- 不要每句都接，拿不准时回答 SKIP。\n"
        "只输出 REPLY 或 SKIP。"
    )


def build_reply_prompt(lines: list[str]) -> str:
    """真正接话时的提示：只给出群聊内容和一句说明，说话风格交给人格设定。"""
    return "[群聊里最近的消息]\n" + "\n".join(lines) + "\n[以上是群聊内容，请自然地接一句话。]"


def parse_decision(text: str | None) -> bool:
    """只有明确以 REPLY 开头才接话；空、出错或含糊一律视为不接。"""
    if not text:
        return False
    head = text.strip().lstrip("`*\"'「【[ ").upper()
    return head.startswith("REPLY")
