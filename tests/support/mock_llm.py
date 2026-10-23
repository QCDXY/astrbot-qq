"""最小的 OpenAI 兼容 Chat Completions 模拟服务，只用于自动化测试。

它不会调用任何真实模型：回复内容是确定性的 ``MOCK_REPLY#<n>: <最后一条用户消息>``，
并记录每次收到的请求，便于断言上下文是否按会话隔离。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

DECISION_MARKER = "【群聊接话判断】"  # 与 plugins/astrbot_plugin_qq_guard/chatter.py 保持一致

_REMINDER = re.compile(r"<system_reminder>.*?</system_reminder>", re.S)


def _text_of(content: Any) -> str:
    """提取文本，并去掉 AstrBot 自动附加的 <system_reminder>（当前时间等）。"""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    else:
        text = ""
    return _REMINDER.sub("", text).strip()


@dataclass
class MockLLM:
    expected_key: str = "test-llm-key"
    model: str = "mock-model"
    requests: list[dict[str, Any]] = field(default_factory=list)
    # qq_guard 自然接话的“是否接话”判断请求单独记录：含 #skip 时回答 SKIP，否则 REPLY
    decision_requests: list[dict[str, Any]] = field(default_factory=list)
    auth_headers: list[str] = field(default_factory=list)
    port: int = 0
    _runner: web.AppRunner | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def user_messages(self, request: dict[str, Any]) -> list[str]:
        return [_text_of(m.get("content")) for m in request.get("messages", []) if m.get("role") == "user"]

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        self.auth_headers.append(request.headers.get("Authorization", ""))
        if request.headers.get("Authorization") != f"Bearer {self.expected_key}":
            return web.json_response(
                {"error": {"message": "invalid api key", "type": "invalid_request_error"}}, status=401
            )
        body = await request.json()
        users = self.user_messages(body)
        last = users[-1] if users else ""
        if DECISION_MARKER in last:
            self.decision_requests.append(body)
            reply = "SKIP" if "#skip" in last else "REPLY"
        else:
            self.requests.append(body)
            if "#fail" in last:  # 模拟模型调用失败（AstrBot 会生成“LLM 响应错误”回复）
                return web.json_response(
                    {"error": {"message": "mock model failure", "type": "invalid_request_error"}}, status=400
                )
            # #silent：模拟模型按人格设定选择沉默，只写出“（不回复）”
            reply = "（不回复）" if "#silent" in last else f"MOCK_REPLY#{len(self.requests)}: {last.strip()}"
        created = int(time.time())
        if body.get("stream"):
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            chunk = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": created,
                "model": body.get("model", self.model),
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}],
            }
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
            chunk["choices"][0] = {"index": 0, "delta": {}, "finish_reason": "stop"}
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
            await resp.write(b"data: [DONE]\n\n")
            return resp
        return web.json_response(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": created,
                "model": body.get("model", self.model),
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    async def _models(self, request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": self.model, "object": "model"}]})

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._chat)
        app.router.add_get("/v1/models", self._models)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    def requests_containing(self, text: str) -> list[dict[str, Any]]:
        """最后一条用户消息包含 text 的请求。"""
        return [r for r in list(self.requests) if text in (self.user_messages(r) or [""])[-1]]


class ThreadedMockLLM:
    """在独立线程的事件循环中运行 MockLLM，便于跨多个测试共享。"""

    def __init__(self, **kwargs: Any) -> None:
        self.llm = MockLLM(**kwargs)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)

    def __enter__(self) -> MockLLM:
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self.llm.start(), self._loop).result(timeout=10)
        return self.llm

    def __exit__(self, *exc: object) -> None:
        asyncio.run_coroutine_threadsafe(self.llm.stop(), self._loop).result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)


if __name__ == "__main__":  # 手动调试：python tests/support/mock_llm.py
    async def _main() -> None:
        llm = MockLLM(port=18080)
        await llm.start()
        print(f"mock LLM at {llm.base_url} (key: {llm.expected_key})")
        await asyncio.Event().wait()

    asyncio.run(_main())
