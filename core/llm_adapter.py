"""LLM adapter — wraps Module/llm backends with tool-calling support.

For tool calling we need structured responses. This adapter provides a unified
think() API that detects tool calls from non-streaming responses.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator

from Module.llm import LLMBackend, LLMFactory

logger = logging.getLogger(__name__)

# 重试配置
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0


@dataclass
class ThoughtResult:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMAdapter:
    """Unified LLM interface with tool-calling support."""

    def __init__(self, model: str | None = None, *,
                 _test_backend: "LLMBackend | None" = None):
        self._requested_model = model or "deepseek/deepseek-chat"

        if _test_backend is not None:
            self._backend: LLMBackend = _test_backend
            logger.info("LLM backend: test double")
            return

        self._backend = LLMFactory.create(self._requested_model)
        logger.info("LLM backend: %s", self._requested_model)

    async def think(self, messages: list[dict], tools: list[dict] | None = None,
                    **kwargs) -> ThoughtResult:
        """Think: call LLM and return structured result (text + optional tool_calls)."""
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await self._backend.chat(messages, **kwargs)

        try:
            parsed = json.loads(response)
            if "tool_calls" in parsed:
                return ThoughtResult(tool_calls=parsed["tool_calls"])
        except (json.JSONDecodeError, TypeError):
            pass

        return ThoughtResult(text=response)

    async def think_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Stream tokens from the LLM. For final answer delivery only (no tools)."""
        async for token in self._backend.chat_stream(messages):
            yield token

    async def stream_think(
        self, messages: list[dict], tools: list[dict] | None = None, **kwargs
    ) -> AsyncIterator[dict]:
        """Stream structured chunks with tool_call detection + retry on 429/5xx.

        Yields dicts: {"content": str | None, "tool_calls": list | None}
        Retries on transient server errors (429, 500-504) with exponential backoff.
        Only retries if streaming hasn't started yet (no duplicate content).
        """
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                if hasattr(self._backend, "stream_think"):
                    started = False
                    async for chunk in self._backend.stream_think(messages, **kwargs):
                        started = True
                        yield chunk
                    return
                else:
                    text = await self._backend.chat(messages, **kwargs)
                    for char in text:
                        yield {"content": char, "tool_calls": None}
                    return
            except Exception as e:
                last_exc = e
                status = self._extract_status(e)
                if status not in _RETRYABLE_STATUS or attempt >= _MAX_RETRIES:
                    raise
                backoff = _RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning("LLM 调用失败 (status=%s), %d/%d 重试 (%.0fs)",
                              status, attempt + 1, _MAX_RETRIES, backoff)
                await asyncio.sleep(backoff)
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _extract_status(exc: Exception) -> int | None:
        """从异常中提取 HTTP 状态码。"""
        if hasattr(exc, 'status_code'):
            return exc.status_code
        # httpx.HTTPStatusError
        if hasattr(exc, 'response') and hasattr(exc.response, 'status_code'):
            return exc.response.status_code
        # openai.APIStatusError
        status = getattr(exc, 'status_code', None)
        if status is not None:
            return status
        return None

    async def close(self) -> None:
        if self._backend:
            await self._backend.close()

    async def reconnect(self) -> None:
        """Recreate the underlying backend's HTTP client. Used on agent restart."""
        if hasattr(self._backend, 'reconnect'):
            await self._backend.reconnect()

    def switch_model(self, new_model_id: str) -> None:
        """热切换 LLM 模型。关闭当前后端，用新 model_id 创建后端。"""
        if hasattr(self._backend, 'close'):
            try:
                import asyncio as _asyncio
                try:
                    loop = _asyncio.get_running_loop()
                    loop.create_task(self._backend.close())
                except RuntimeError:
                    pass
            except Exception:
                pass
        self._requested_model = new_model_id
        self._backend = LLMFactory.create(new_model_id)
        logger.info("LLM switched to: %s", new_model_id)

    @property
    def model(self) -> str:
        return self._requested_model
