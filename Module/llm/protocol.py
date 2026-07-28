"""LLM 后端抽象接口。所有大模型后端必须实现此协议。"""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any


class LLMBackend(ABC):
    """大模型后端抽象基类。

    消息格式统一采用 OpenAI 格式 ``[{"role": "...", "content": "..."}]``，
    各后端内部按需转换。**kwargs 透传 temperature、max_tokens 等参数。
    """

    @abstractmethod
    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        """非流式对话 — 返回完整回复文本。

        Args:
            messages: OpenAI 格式的消息列表，
                如 [{"role": "user", "content": "你好"}]。
            **kwargs: 透传给底层 API 的参数（temperature、max_tokens 等）。

        Returns:
            模型生成的完整回复文本。
        """
        ...

    @abstractmethod
    async def chat_stream(
        self, messages: list[dict], **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """流式对话 — 逐 token 产出回复文本。

        Args:
            messages: OpenAI 格式的消息列表。
            **kwargs: 透传给底层 API 的参数（temperature、max_tokens 等）。

        Yields:
            逐个 token 的文本片段。
        """
        ...
        yield ""  # type: ignore[misc]  # pragma: no cover — 让抽象方法成为生成器

    async def stream_think(
        self, messages: list[dict], **kwargs: Any,
    ) -> AsyncGenerator[dict, None]:
        """流式思考 — 逐 chunk 产出结构化 dict，支持 tool_calls 检测和 usage 统计。

        每个 chunk: {"content": str | None, "tool_calls": list | None}
        最后一个 chunk 可能包含可选的 "usage" 键:
          {"content": None, "tool_calls": None, "usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}}
        默认实现回退到 chat()，逐字符 yield 纯文本（无 tool_calls）。
        支持 tool calling 的 backend 应覆盖此方法。

        Args:
            messages: OpenAI 格式的消息列表。
            **kwargs: 透传给底层 API 的参数。

        Yields:
            结构化 dict，包含 content（文本片段）、tool_calls（工具调用 delta）
            以及可选的 usage（token 统计）。
        """
        text = await self.chat(messages, **kwargs)
        for char in text:
            yield {"content": char, "tool_calls": None}

    @abstractmethod
    async def test(self) -> dict:
        """测试连通性。

        Returns:
            {"ok": bool, "latency_ms": int, "error": str}
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭底层连接。"""
        ...
