"""qq_guard 的纯逻辑部分，不依赖 AstrBot，便于单元测试。"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum


class Verdict(StrEnum):
    ALLOW = "allow"
    SELF_MESSAGE = "self_message"
    GROUP_NOT_ALLOWED = "group_not_allowed"
    RATE_LIMITED = "rate_limited"


def parse_id_list(raw: Iterable[object] | str | None) -> frozenset[str]:
    """接受列表或逗号/空白分隔的字符串，返回去重后的 ID 集合。"""
    if raw is None:
        return frozenset()
    items: Iterable[object]
    if isinstance(raw, str):
        items = raw.replace("，", ",").replace(",", " ").split()
    else:
        items = raw
    return frozenset(s for s in (str(i).strip() for i in items) if s)


class SlidingWindowLimiter:
    """按 key 的滑动窗口计数器：window 秒内最多 limit 次。limit<=0 表示不限制。"""

    def __init__(
        self,
        limit: int,
        window: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10000,
    ) -> None:
        self.limit = int(limit)
        self.window = float(window)
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        return q

    def hit(self, key: str) -> bool:
        """记录一次请求；返回 True 表示放行，False 表示超限（超限请求不计数）。"""
        if self.limit <= 0 or self.window <= 0:
            return True
        now = self._clock()
        q = self._prune(key, now)
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self._hits) > self._max_keys:
            self._gc(now)
        return True

    def remaining(self, key: str) -> int | None:
        if self.limit <= 0 or self.window <= 0:
            return None
        return max(0, self.limit - len(self._prune(key, self._clock())))

    def retry_after(self, key: str) -> float:
        q = self._prune(key, self._clock())
        if len(q) < self.limit or not q:
            return 0.0
        return max(0.0, self.window - (self._clock() - q[0]))

    def _gc(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] >= self.window]:
            del self._hits[k]


# 开启 tool_use 后 AstrBot 会自动挂载的内置工具中，不适合群聊/公开场景的：
# send_message_to_user 可向任意会话发消息、可发送本地文件（例如含密钥的配置）；
# future_task 可让任何人创建定时任务。
DEFAULT_BLOCKED_LLM_TOOLS = frozenset({"send_message_to_user", "future_task"})


@dataclass
class GuardConfig:
    group_allowlist: frozenset[str] = field(default_factory=frozenset)
    user_rate_limit_count: int = 0
    user_rate_limit_window: int = 60
    notify_on_limit: bool = True
    blocked_llm_tools: frozenset[str] = DEFAULT_BLOCKED_LLM_TOOLS
    drop_silent_replies: bool = True
    hide_unprompted_errors: bool = True

    @classmethod
    def from_mapping(cls, m: dict | None) -> GuardConfig:
        m = m or {}
        blocked = m.get("blocked_llm_tools")
        return cls(
            group_allowlist=parse_id_list(m.get("group_allowlist")),
            user_rate_limit_count=_as_int(m.get("user_rate_limit_count"), 0),
            user_rate_limit_window=_as_int(m.get("user_rate_limit_window"), 60),
            notify_on_limit=bool(m.get("notify_on_limit", True)),
            # 未配置时使用默认屏蔽列表；显式配置为空列表表示不屏蔽
            blocked_llm_tools=DEFAULT_BLOCKED_LLM_TOOLS if blocked is None else parse_id_list(blocked),
            drop_silent_replies=bool(m.get("drop_silent_replies", True)),
            hide_unprompted_errors=bool(m.get("hide_unprompted_errors", True)),
        )


# 人格设定里用“（不回复）”表示保持沉默，模型有时会把它原样写出来
_SILENT_WORDS = frozenset({"不回复"})
_SILENT_TRIM = " \t\r\n()（）[]【】「」『』<>《》.。…!！~～"

# AstrBot 把模型调用失败转换成的回复文本（未设置人格自定义错误消息时）
_LLM_ERROR_PREFIXES = ("LLM 响应错误", "Error occurred during AI execution")


def is_silent_reply(text: str | None) -> bool:
    """回复是否只有“（不回复）”这类沉默标记（含各种括号、空白与句末标点）。"""
    if not text:
        return False
    return text.strip(_SILENT_TRIM) in _SILENT_WORDS


def is_llm_error_reply(text: str | None) -> bool:
    return bool(text) and text.lstrip().startswith(_LLM_ERROR_PREFIXES)


def should_drop_reply(text: str | None, *, unprompted: bool, config: GuardConfig) -> str | None:
    """决定是否不发送这条回复，返回原因（None 表示照常发送）。

    unprompted：机器人自己发起的回复（群聊自然接话、AstrBot 内置主动回复），
    即群聊中没有被 @、回复或指令唤醒的消息。被 @ 或私聊时出错仍要让用户看到。
    """
    if config.drop_silent_replies and is_silent_reply(text):
        return "silent"
    if config.hide_unprompted_errors and unprompted and is_llm_error_reply(text):
        return "error"
    return None


def strip_blocked_tools(tool_set, blocked: frozenset[str]) -> list[str]:
    """从工具集中移除被屏蔽的工具，返回实际移除的名字。tool_set 需提供 names()/remove_tool()。"""
    if tool_set is None:
        return []
    removed = [name for name in tool_set.names() if name in blocked]
    for name in removed:
        tool_set.remove_tool(name)
    return removed


def _as_int(v: object, default: int) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass
class MessageInfo:
    sender_id: str
    self_id: str
    group_id: str  # 私聊为空字符串
    is_admin: bool


class Guard:
    def __init__(self, config: GuardConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.limiter = SlidingWindowLimiter(
            config.user_rate_limit_count, config.user_rate_limit_window, clock=clock
        )
        # 每个用户每个窗口内只提示一次，避免提示本身变成刷屏
        self._notified = SlidingWindowLimiter(1, config.user_rate_limit_window, clock=clock)

    def group_allowed(self, group_id: str) -> bool:
        return not group_id or not self.config.group_allowlist or group_id in self.config.group_allowlist

    def check(self, msg: MessageInfo) -> Verdict:
        if msg.self_id and msg.sender_id == msg.self_id:
            return Verdict.SELF_MESSAGE
        if msg.is_admin:
            return Verdict.ALLOW
        if not self.group_allowed(msg.group_id):
            return Verdict.GROUP_NOT_ALLOWED
        if not self.limiter.hit(msg.sender_id):
            return Verdict.RATE_LIMITED
        return Verdict.ALLOW

    def should_notify(self, sender_id: str) -> bool:
        return self.config.notify_on_limit and self._notified.hit(sender_id)
