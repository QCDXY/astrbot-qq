"""模拟 NapCat：以 OneBot v11 反向 WebSocket 客户端身份连接 AstrBot。

- 连接时携带与 NapCat 相同的请求头（X-Self-ID / X-Client-Role / Authorization）。
- 可以推送私聊、群聊消息事件。
- 对 AstrBot 发来的 API 调用（send_private_msg、send_group_msg 等）返回成功，
  并把调用记录下来，测试据此判断机器人“回复”了什么。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import dataclass, field
from typing import Any

import websockets

SEND_ACTIONS = {"send_private_msg", "send_group_msg", "send_msg"}


def text_of(message: Any) -> str:
    """把 OneBot 消息段数组或字符串转成纯文本。"""
    if isinstance(message, str):
        return message
    out = []
    for seg in message or []:
        if seg.get("type") == "text":
            out.append(seg.get("data", {}).get("text", ""))
    return "".join(out)


@dataclass
class MockNapCat:
    url: str
    token: str
    self_id: int = 10001
    calls: list[dict[str, Any]] = field(default_factory=list)
    _ws: Any = None
    _reader: asyncio.Task | None = None
    _msg_ids: itertools.count = field(default_factory=lambda: itertools.count(1000))
    _new_call: asyncio.Event = field(default_factory=asyncio.Event)

    async def connect(self, token: str | None = None) -> None:
        headers = {
            "X-Self-ID": str(self.self_id),
            "X-Client-Role": "Universal",
            "User-Agent": "MockNapCat/1.0",
        }
        tok = self.token if token is None else token
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        self._ws = await websockets.connect(self.url, additional_headers=headers, open_timeout=10)
        self._reader = asyncio.create_task(self._read_loop())
        # 与 NapCat 一致：连接后推送 lifecycle 元事件
        await self._send(
            {"time": int(time.time()), "self_id": self.self_id, "post_type": "meta_event",
             "meta_event_type": "lifecycle", "sub_type": "connect"}
        )

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        self._ws = None
        self._reader = None

    async def _send(self, obj: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(obj, ensure_ascii=False))

    def _api_result(self, action: str, params: dict[str, Any]) -> Any:
        if action in SEND_ACTIONS:
            return {"message_id": next(self._msg_ids)}
        if action == "get_login_info":
            return {"user_id": self.self_id, "nickname": "MockBot"}
        if action == "get_group_info":
            return {"group_id": params.get("group_id"), "group_name": "测试群", "member_count": 3, "max_member_count": 200}
        if action in ("get_group_member_info", "get_stranger_info"):
            return {"user_id": params.get("user_id"), "nickname": "tester", "card": "", "role": "member"}
        if action == "get_msg":
            return {"message_id": params.get("message_id"), "message": [], "sender": {}}
        return {}

    async def _read_loop(self) -> None:
        async for raw in self._ws:
            data = json.loads(raw)
            if "action" not in data:
                continue
            action = data["action"]
            params = data.get("params") or {}
            self.calls.append({"action": action, "params": params, "t": time.monotonic()})
            self._new_call.set()
            reply = {"status": "ok", "retcode": 0, "data": self._api_result(action, params)}
            if "echo" in data:
                reply["echo"] = data["echo"]
            await self._send(reply)

    # ---- 事件 ----
    async def private_message(self, user_id: int, text: str, nickname: str = "tester") -> None:
        mid = next(self._msg_ids)
        await self._send(
            {
                "time": int(time.time()), "self_id": self.self_id, "post_type": "message",
                "message_type": "private", "sub_type": "friend", "message_id": mid,
                "user_id": user_id, "message": [{"type": "text", "data": {"text": text}}],
                "raw_message": text, "font": 0,
                "sender": {"user_id": user_id, "nickname": nickname, "sex": "unknown", "age": 0},
            }
        )

    async def group_message(self, group_id: int, user_id: int, text: str, at_bot: bool = False) -> None:
        segs: list[dict[str, Any]] = []
        raw = text
        if at_bot:
            segs.append({"type": "at", "data": {"qq": str(self.self_id)}})
            raw = f"[CQ:at,qq={self.self_id}] {text}"
        segs.append({"type": "text", "data": {"text": (" " + text) if at_bot else text}})
        mid = next(self._msg_ids)
        await self._send(
            {
                "time": int(time.time()), "self_id": self.self_id, "post_type": "message",
                "message_type": "group", "sub_type": "normal", "message_id": mid,
                "group_id": group_id, "user_id": user_id, "message": segs, "raw_message": raw,
                "font": 0, "sender": {"user_id": user_id, "nickname": "tester", "card": "", "role": "member"},
            }
        )

    # ---- 断言辅助 ----
    def sent_messages(self) -> list[dict[str, Any]]:
        out = []
        for c in self.calls:
            if c["action"] in SEND_ACTIONS:
                p = c["params"]
                out.append({
                    "action": c["action"],
                    "user_id": p.get("user_id"),
                    "group_id": p.get("group_id"),
                    "text": text_of(p.get("message")),
                    "t": c["t"],
                })
        return out

    async def wait_for_send(self, predicate, timeout: float = 30.0) -> dict[str, Any]:  # noqa: ASYNC109
        deadline = time.monotonic() + timeout
        while True:
            for m in self.sent_messages():
                if predicate(m):
                    return m
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"在 {timeout}s 内没有等到期望的回复；已发送：{self.sent_messages()}")
            self._new_call.clear()
            try:
                await asyncio.wait_for(self._new_call.wait(), timeout=min(remaining, 0.5))
            except TimeoutError:
                pass
