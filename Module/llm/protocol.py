"""LLM 后端抽象接口。所有大模型后端必须实现此协议。"""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator


class LLMBackend(ABC):
    """大模型后端抽象基类。

    消息格式统一采用 OpenAI 格式 ``[{"role": "...", "content": "..."}]``，
    各后端内部按需转换。**kwargs 透传 temperature、max_tokens 等参数。
    """

    @abstractmethod
    async def chat(self, messages: list[dict], **kwargs) -> str:
        """非流式对话 — 返回完整回复文本。"""
        ...

    @abstractmethod
    async def chat_stream(self, messages: list[dict], **kwargs) -> AsyncGenerator[str, None]:
        """流式对话 — 逐 token 产出回复文本。"""
        ...
        yield ""  # type: ignore[misc]  # pragma: no cover — 让抽象方法成为生成器

    async def stream_think(
        self, messages: list[dict], **kwargs
    ) -> AsyncGenerator[dict, None]:
        """流式思考 — 逐 chunk 产出结构化 dict，支持 tool_calls 检测。

        每个 chunk: {"content": str | None, "tool_calls": list | None}
        默认实现回退到 chat()，逐字符 yield 纯文本（无 tool_calls）。
        支持 tool calling 的 backend 应覆盖此方法。
        """
        text = await self.chat(messages, **kwargs)
        for char in text:
            yield {"content": char, "tool_calls": None}

    @abstractmethod
    async def close(self) -> None:
        """关闭底层连接。"""
        ...
