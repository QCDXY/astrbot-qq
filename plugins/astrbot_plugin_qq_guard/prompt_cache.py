"""Anthropic（Claude）对话历史的提示缓存断点，纯逻辑部分不依赖 AstrBot，便于单元测试。

AstrBot 4.29.1 的 Anthropic 适配器只在系统提示词的最后一块上放一个 cache_control，
只缓存“工具定义 + 系统提示词”，对话历史每轮都按全价重新计算
（上游 issue AstrBotDevs/AstrBot#9388，尚未修复）。QQ 机器人的人格提示词通常很短，
而群聊上下文和多轮历史很长，于是缓存命中率只有个位数。

这里在 AstrBot 放好系统断点之后，再在最后一条消息的最后一个内容块上加一个断点：
每轮请求都把整个对话写入缓存，下一轮只需按缓存价读取之前的部分。
断点只加在本次请求的副本上，不会写进 AstrBot 保存的会话历史。
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

MAX_BREAKPOINTS = 4  # Anthropic 每个请求最多 4 个 cache_control
TTL_CHOICES = ("5m", "1h")
# 可以放 cache_control 的内容块类型（thinking 块不行）
_CACHEABLE_TYPES = {"text", "image", "document", "tool_use", "tool_result"}

_skip = contextvars.ContextVar("qq_guard_skip_history_cache", default=False)


@contextmanager
def skip_history_cache() -> Iterator[None]:
    """在其中发起的请求不加历史断点（一次性请求，内容不会被下一次请求复用）。"""
    token = _skip.set(True)
    try:
        yield
    finally:
        _skip.reset(token)


def normalize_ttl(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in TTL_CHOICES else "5m"


def cache_control(ttl: str) -> dict[str, str]:
    return {"type": "ephemeral", "ttl": "1h"} if ttl == "1h" else {"type": "ephemeral"}


def _blocks(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _count_breakpoints(payloads: dict[str, Any]) -> int:
    count = 0
    for block in [*_blocks(payloads.get("tools")), *_blocks(payloads.get("system"))]:
        if isinstance(block, dict) and "cache_control" in block:
            count += 1
    for msg in _blocks(payloads.get("messages")):
        content = msg.get("content") if isinstance(msg, dict) else None
        for block in _blocks(content):
            if isinstance(block, dict) and "cache_control" in block:
                count += 1
    return count


def _as_blocks(content: Any) -> list[Any] | None:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return list(content)
    return None


def _markable(block: Any) -> bool:
    if not isinstance(block, dict) or block.get("type") not in _CACHEABLE_TYPES:
        return False
    # 空文本块不能带 cache_control（API 会返回 400）
    return block.get("type") != "text" or bool(str(block.get("text") or "").strip())


def add_history_breakpoint(payloads: dict[str, Any], ttl: str = "5m") -> bool:
    """在 Anthropic 请求的最后一条消息上加缓存断点。返回是否加了断点。

    - 已有任何消息带 cache_control（插件或新版 AstrBot 自己加的）时不再重复添加；
    - 断点数已满 4 个时不添加；
    - 字符串形式的消息内容统一转换为单个 text 块（与字符串等价），保证各轮请求形状一致；
    - 只替换 payloads 里的消息/内容块副本，不修改调用方的原始对象。
    """
    if _skip.get():
        return False
    messages = payloads.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    if _count_breakpoints(payloads) >= MAX_BREAKPOINTS:
        return False
    if any(
        isinstance(block, dict) and "cache_control" in block
        for msg in messages
        if isinstance(msg, dict)
        for block in _blocks(msg.get("content"))
    ):
        return False

    new_messages: list[Any] = []
    for msg in messages:
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            msg = {**msg, "content": _as_blocks(msg["content"])}
        new_messages.append(msg)

    last = new_messages[-1]
    content = _as_blocks(last.get("content")) if isinstance(last, dict) else None
    if not content:
        return False
    for i in range(len(content) - 1, -1, -1):
        if _markable(content[i]):
            content[i] = {**content[i], "cache_control": cache_control(ttl)}
            break
    else:
        return False

    new_messages[-1] = {**last, "content": content}
    payloads["messages"] = new_messages
    if ttl == "1h":
        _upgrade_earlier_breakpoints(payloads)
    return True


_PATCHED_METHOD = "_apply_explicit_prompt_cache_breakpoints"
_ORIGINAL_ATTR = "_qq_guard_original_breakpoints"


def install(provider_cls: type, ttl: str = "5m") -> bool:
    """包装 provider_cls 放置系统断点的方法，使其之后再加历史断点。可重复调用（更新 TTL）。

    返回 False 表示该版本的 AstrBot 没有这个方法（实现已变化），此时不做任何修改。
    """
    original = provider_cls.__dict__.get(_ORIGINAL_ATTR) or provider_cls.__dict__.get(_PATCHED_METHOD)
    func = getattr(original, "__func__", None)
    if not isinstance(original, classmethod) or func is None:
        return False
    ttl = normalize_ttl(ttl)

    def apply_breakpoints(cls: type, payloads: dict) -> None:
        func(cls, payloads)
        add_history_breakpoint(payloads, ttl)

    setattr(provider_cls, _ORIGINAL_ATTR, original)
    setattr(provider_cls, _PATCHED_METHOD, classmethod(apply_breakpoints))
    return True


def uninstall(provider_cls: type) -> None:
    original = provider_cls.__dict__.get(_ORIGINAL_ATTR)
    if original is not None:
        setattr(provider_cls, _PATCHED_METHOD, original)
        delattr(provider_cls, _ORIGINAL_ATTR)


def _upgrade_earlier_breakpoints(payloads: dict[str, Any]) -> None:
    """1 小时断点之前不能有 5 分钟断点（长 TTL 必须在前），把工具/系统上的断点同样升级为 1 小时。"""
    for key in ("tools", "system"):
        blocks = payloads.get(key)
        if not isinstance(blocks, list):
            continue
        payloads[key] = [
            {**b, "cache_control": cache_control("1h")} if isinstance(b, dict) and "cache_control" in b else b
            for b in blocks
        ]
