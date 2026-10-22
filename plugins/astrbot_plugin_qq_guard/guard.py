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


@dataclass
class GuardConfig:
    group_allowlist: frozenset[str] = field(default_factory=frozenset)
    user_rate_limit_count: int = 0
    user_rate_limit_window: int = 60
    notify_on_limit: bool = True

    @classmethod
    def from_mapping(cls, m: dict | None) -> GuardConfig:
        m = m or {}
        return cls(
            group_allowlist=parse_id_list(m.get("group_allowlist")),
            user_rate_limit_count=_as_int(m.get("user_rate_limit_count"), 0),
            user_rate_limit_window=_as_int(m.get("user_rate_limit_window"), 60),
            notify_on_limit=bool(m.get("notify_on_limit", True)),
        )


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
