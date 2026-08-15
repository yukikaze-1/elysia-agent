"""记忆域 LLM 窄接口 — MemoryConsolidator / MemoryReflector 的结构化依赖。

CORE-MOD-P1-02：memory 不再 import core.adapter 的具体 LLMAdapter；
任何提供 ``think(messages, **kwargs)`` 的异步实现均满足本 Protocol。
"""

from typing import Any, Protocol


class MemoryLLM(Protocol):
    """记忆域所需的最小 LLM 接口。

    ``think`` 返回对象仅需暴露 ``text`` 字段（memory 域不消费 tool_calls）。
    """

    async def think(self, messages: list[dict], **kwargs: Any) -> Any:
        """非流式调用 LLM，返回带 ``text`` 属性的结果对象。"""
        ...
