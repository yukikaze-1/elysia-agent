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

from core.adapter.tool_call_parser import normalize_tool_calls
from core.schema.tool import ToolCall

logger = logging.getLogger(__name__)

# 重试配置
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0


@dataclass
class ThoughtResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMAdapter:
    """Unified LLM interface with tool-calling support."""

    def __init__(self, model: str | None = None, *,
                 _test_backend: "LLMBackend | None" = None):
        """初始化 LLM 适配器。

        Args:
            model: LLM 模型标识符（如 "deepseek/deepseek-chat"），为 None 时使用默认值。
            _test_backend: 测试用后端实例，为 None 时通过工厂创建。
        """
        self._requested_model = model or "deepseek/deepseek-chat"

        if _test_backend is not None:
            self._backend: LLMBackend = _test_backend
            logger.info("LLM backend: test double")
            return

        self._backend = LLMFactory.create(self._requested_model)
        logger.info("LLM backend: %s", self._requested_model)

    async def think(self, messages: list[dict], tools: list[dict] | None = None,
                    **kwargs) -> ThoughtResult:
        """调用 LLM 并返回结构化结果（text + 可选 tool_calls）。

        Args:
            messages: LLM 对话消息列表。
            tools: 可选的工具定义列表，非 None 时启用 tool_choice=auto。
            **kwargs: 传递给后端 chat() 的额外参数。

        Returns:
            ThoughtResult: 包含 text 和 tool_calls 的结构化结果。
        """
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await self._backend.chat(messages, **kwargs)

        try:
            parsed = json.loads(response)
            if "tool_calls" in parsed:
                return ThoughtResult(tool_calls=normalize_tool_calls(parsed["tool_calls"]))
        except (json.JSONDecodeError, TypeError):
            pass

        return ThoughtResult(text=response)

    async def think_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """从 LLM 流式获取 token。仅用于最终回答输出（不支持工具调用）。

        Args:
            messages: LLM 对话消息列表。

        Returns:
            AsyncIterator[str]: 流式文本 token 迭代器。
        """
        async for token in self._backend.chat_stream(messages):
            yield token

    async def stream_think(
        self, messages: list[dict], tools: list[dict] | None = None, **kwargs
    ) -> AsyncIterator[dict]:
        """流式输出结构化 chunk，支持工具调用检测 + 429/5xx 重试。

        Yields dicts: {"content": str | None, "tool_calls": list[dict] | None}
        流最后一个 chunk 可能包含可选的 "usage" 键:
          {"content": None, "tool_calls": None, "usage": {"input_tokens": int, "output_tokens": int, "total_tokens": int, "cached_tokens": int | None, "reasoning_tokens": int | None}}
        对临时服务器错误（429, 500-504）以指数退避重试。
        仅在流尚未开始输出时重试（避免重复内容）。

        Args:
            messages: LLM 对话消息列表。
            tools: 可选的工具定义列表，非 None 时启用 tool_choice=auto。
            **kwargs: 传递给后端的额外参数。

        Returns:
            AsyncIterator[dict]: 结构化 chunk 迭代器，每个 chunk 包含 content 和 tool_calls 键。
        """
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                started = False
                if hasattr(self._backend, "stream_think"):
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
                if status not in _RETRYABLE_STATUS or attempt >= _MAX_RETRIES or started:
                    if started:
                        logger.warning("LLM 流已开始输出，跳过重试（避免重复内容）")
                    raise
                backoff = _RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning("LLM 调用失败 (status=%s), %d/%d 重试 (%.0fs)",
                              status, attempt + 1, _MAX_RETRIES, backoff)
                await asyncio.sleep(backoff)
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _extract_status(exc: Exception) -> int | None:
        """从异常中提取 HTTP 状态码。

        Args:
            exc: 异常对象（如 httpx.HTTPStatusError 或 openai.APIStatusError）。

        Returns:
            int | None: HTTP 状态码，无法提取时返回 None。
        """
        # httpx.HTTPStatusError / openai.APIStatusError
        status: int | None = getattr(exc, 'status_code', None)  # type: ignore[union-attr]
        if status is not None:
            return status
        # httpx.HTTPStatusError — response.status_code
        resp: object | None = getattr(exc, 'response', None)  # type: ignore[union-attr]
        if resp is not None:
            return getattr(resp, 'status_code', None)  # type: ignore[union-attr]
        return None

    async def close(self) -> None:
        """关闭底层 LLM 后端的连接。"""
        if self._backend:
            await self._backend.close()

    async def reconnect(self) -> None:
        """Recreate the underlying backend's HTTP client. Used on agent restart."""
        # LLMBackend protocol 未声明 reconnect，但部分后端实现了它
        reconnect_meth = getattr(self._backend, 'reconnect', None)  # type: ignore[union-attr]
        if reconnect_meth is not None:
            await reconnect_meth()

    def switch_model(self, new_model_id: str, **kwargs) -> None:
        """热切换 LLM 模型。关闭当前后端，用新 model_id 创建后端。

        Args:
            new_model_id: 新的模型标识符（如 "deepseek/deepseek-v4-flash"）。
            **kwargs: 透传给 LLMFactory.create（如 custom provider 的 base_url/api_key）。
        """
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
        self._backend = LLMFactory.create(new_model_id, **kwargs)
        logger.info("LLM switched to: %s", new_model_id)

    @property
    def model(self) -> str:
        return self._requested_model

    def get_raw_client(self) -> "object | None":
        """返回底层异步 HTTP 客户端，供 WorkflowEngine Planner/Executor 使用。

        Returns:
            openai.AsyncOpenAI | httpx.AsyncClient | None
            如果后端不支持或客户端未初始化，返回 None。
        """
        backend = getattr(self, '_backend', None)
        if backend is not None:
            return getattr(backend, '_client', None)
        return None
