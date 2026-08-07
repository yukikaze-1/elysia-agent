"""用于测试的最小 LLM 后端 —— 响应可控，无 API 调用。

提供：
- Token 级流式输出（而非逐字符）
- 可配置的故障注入（HTTP 错误、超时、断连）
"""

from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

from Module.llm import LLMBackend

# 简单分词：按词边界 + 标点切分，以支持 CJK 感知输出
_TOKEN_RE = re.compile(r"([一-鿿]|[\w]+|[^\w\s])")  # noqa: RUF001


class FakeLLMBackend(LLMBackend):
    """用于测试的可配置假 LLM。返回你设置的任何内容。

    Usage:
        # 正常：token 级流式输出
        llm = FakeLLMBackend(response="你好，世界")

        # 故障注入：
        llm = FakeLLMBackend(response="ok", fault="http_429")
        llm = FakeLLMBackend(response="ok", fault="timeout")
        llm = FakeLLMBackend(response="ok", fault="disconnect")
        llm = FakeLLMBackend(response="ok", fault="http_503")
    """

    def __init__(
        self,
        response: str = "test response",
        fault: str | None = None,
        fault_on_call: int = 1,
    ):
        """初始化假后端。

        参数：
            response: chat/chat_stream 返回的文本。
            fault: 可选注入的故障："http_429"、"http_503"、
                   "timeout"、"disconnect"。
            fault_on_call: 第几次调用触发故障（从 1 开始）。
        """
        self.response = response
        self.fault = fault
        self.fault_on_call = fault_on_call
        self._call_count = 0
        self._model = "fake/test"

    def _maybe_inject_fault(self) -> None:
        """检查并注入配置的故障。"""
        self._call_count += 1
        if self.fault and self._call_count >= self.fault_on_call:
            if self.fault == "http_429":
                from Module.llm.errors import RateLimitError
                raise RateLimitError("Fake 429 Too Many Requests")
            elif self.fault == "http_503":
                from Module.llm.errors import ServiceUnavailableError
                raise ServiceUnavailableError("Fake 503 Service Unavailable")
            elif self.fault == "timeout":
                raise asyncio.TimeoutError("Fake timeout")
            elif self.fault == "disconnect":
                raise ConnectionError("Fake connection lost")
            # 首次注入后重置，使后续调用可以成功
            self.fault = None

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """将文本切成类似 token 的块（词 / CJK 字符级别）。"""
        tokens = _TOKEN_RE.findall(text)
        return tokens if tokens else list(text)

    async def chat(self, messages: list[dict], **kwargs) -> str:
        """返回预设响应。支持故障注入。"""
        self._maybe_inject_fault()
        return self.response

    async def chat_stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """按 token 逐个产出响应（而非逐字符）。
        支持故障注入。"""
        self._maybe_inject_fault()
        for token in self._tokenize(self.response):
            yield token

    async def test(self) -> dict:
        """健康检查 —— 假后端始终为 ok。"""
        return {"ok": True, "latency_ms": 0}

    async def close(self) -> None:
        """无操作 close。"""
        pass
