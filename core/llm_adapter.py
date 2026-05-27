"""LLM adapter — wraps Module/llm backends with tool-calling support.

For tool calling we need structured responses. This adapter provides a unified
think() API that detects tool calls from non-streaming responses.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import AsyncIterator

from Module.llm import LLMBackend, LLMFactory

logger = logging.getLogger(__name__)


@dataclass
class ThoughtResult:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMAdapter:
    """Unified LLM interface with tool-calling support.

    Requires DEEPSEEK_API_KEY env var. Raises RuntimeError if not set.
    """

    def __init__(self, model: str | None = None, *,
                 _test_backend: "LLMBackend | None" = None):
        self._requested_model = model or "deepseek/deepseek-chat"

        if _test_backend is not None:
            self._backend: LLMBackend = _test_backend
            logger.info("LLM backend: test double")
            return

        if not os.getenv("DEEPSEEK_API_KEY", ""):
            raise RuntimeError(
                "DEEPSEEK_API_KEY not set. Set the env var to use the LLM."
            )

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
        """Stream structured chunks with tool_call detection.

        Yields dicts: {"content": str | None, "tool_calls": list | None}
        Works with real backends via stream_think.
        """
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if hasattr(self._backend, "stream_think"):
            async for chunk in self._backend.stream_think(messages, **kwargs):
                yield chunk
        else:
            text = await self._backend.chat(messages, **kwargs)
            for char in text:
                yield {"content": char, "tool_calls": None}

    async def close(self) -> None:
        if self._backend:
            await self._backend.close()

    @property
    def model(self) -> str:
        return self._requested_model
