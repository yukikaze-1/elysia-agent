"""LLM adapter — wraps Module/llm backends with tool-calling + mock fallback.

The current Module/llm protocol yields plain-text tokens from chat_stream().
For tool calling we need structured responses. This adapter provides a unified
think() API that detects tool calls from non-streaming responses, and falls
back to a mock LLM when no real backend is available.
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


class MockLLMBackend(LLMBackend):
    """Mock LLM backend for demo without API keys.

    Returns realistic-looking responses and simulates tool calling
    by detecting trigger phrases in user input.
    """

    RESPONSES = {
        "时间": "现在是下午三点二十分，阳光正好呢。",
        "计划": "让我查一下你的记忆库。找到了，上周我们约定周末去爬山，别忘了准备登山鞋。",
        "你好": "你好呀！今天过得怎么样？",
        "名字": "我叫Elysia，是你的桌面助手和伙伴。",
        "天气": "抱歉，我还不能查实时天气，但窗外看起来不错哦。",
        "心情": "和你聊天的时候心情总是最好的！",
    }

    TOOL_TRIGGERS = {
        "回忆": ("search_memory", {"query": "约定 计划 爬山", "limit": 5}),
        "查询": ("search_memory", {"query": "计划 约定", "limit": 3}),
        "记忆": ("search_memory", {"query": "喜好 学习", "limit": 5}),
        "搜索": ("search_files", {"path": "/home", "pattern": "*.txt"}),
    }

    def __init__(self, model: str = "mock/elysia", **kwargs):
        self._model = model

    async def chat(self, messages: list[dict], **kwargs) -> str:
        user_msg = messages[-1]["content"] if messages else ""
        tools = kwargs.get("tools")

        # Check tool triggers
        if tools:
            for trigger, (name, args) in self.TOOL_TRIGGERS.items():
                if trigger in user_msg:
                    return json.dumps({
                        "tool_calls": [{
                            "id": "mock_call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                        }]
                    }, ensure_ascii=False)

        # Find best matching response
        for keyword, response in self.RESPONSES.items():
            if keyword in user_msg:
                return response

        return await asyncio.to_thread(self._generic_reply, user_msg)

    async def chat_stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        text = await self.chat(messages, **kwargs)

        # If this is a tool-call response (JSON), yield it as structured
        if text.startswith('{"tool_calls"'):
            yield text
            return

        # Simulate streaming by yielding characters
        for char in text:
            yield char
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        pass

    @staticmethod
    def _generic_reply(msg: str) -> str:
        return f"嗯，关于「{msg[:20]}」这件事...我觉得挺有意思的。要不要再详细说说？"


class LLMAdapter:
    """Unified LLM interface with tool-calling support and fallback.

    Tries to create a real backend; falls back to MockLLMBackend.
    Handles tool-calling by parsing non-streaming responses for tool_calls JSON.
    If the real backend fails at API call time, switches to mock automatically.
    """

    def __init__(self):
        self._backend: LLMBackend
        self._using_mock = False

        # Check if required API key is available before attempting real backend
        deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
        if deepseek_key:
            try:
                self._backend = LLMFactory.create("deepseek/deepseek-chat")
                logger.info("LLM backend: deepseek/deepseek-chat (real)")
                self._using_mock = False
                return
            except Exception as e:
                logger.info("Real LLM creation failed (%s), using mock", e)
        else:
            logger.info("DEEPSEEK_API_KEY not set, using mock LLM")

        self._backend = MockLLMBackend()
        self._using_mock = True

    async def think(self, messages: list[dict], tools: list[dict] | None = None) -> ThoughtResult:
        """Think: call LLM and return structured result (text + optional tool_calls)."""
        kwargs: dict = {}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await self._call_with_fallback(messages, **kwargs)

        # Try to parse tool calls from response
        try:
            parsed = json.loads(response)
            if "tool_calls" in parsed:
                return ThoughtResult(tool_calls=parsed["tool_calls"])
        except (json.JSONDecodeError, TypeError):
            pass

        return ThoughtResult(text=response)

    async def think_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Stream tokens from the LLM. For final answer delivery only (no tools)."""
        try:
            async for token in self._backend.chat_stream(messages):
                yield token
        except Exception as e:
            if not self._using_mock:
                logger.info("Real LLM stream failed (%s), switching to mock", type(e).__name__)
                self._switch_to_mock()
                async for token in self._backend.chat_stream(messages):
                    yield token
            else:
                raise

    async def stream_think(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> AsyncIterator[dict]:
        """Stream structured chunks with tool_call detection.

        Yields dicts: {"content": str | None, "tool_calls": list | None}
        Works with both real (via stream_think) and mock (simulated) backends.
        """
        kwargs: dict = {}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            if hasattr(self._backend, "stream_think"):
                async for chunk in self._backend.stream_think(messages, **kwargs):
                    yield chunk
            else:
                # Mock backend: use non-streaming then simulate streaming
                text = await self._backend.chat(messages, **kwargs)
                if text.startswith('{"tool_calls"'):
                    try:
                        parsed = json.loads(text)
                        yield {"content": None, "tool_calls": parsed.get("tool_calls", [])}
                    except json.JSONDecodeError:
                        for char in text:
                            yield {"content": char, "tool_calls": None}
                    return
                for char in text:
                    yield {"content": char, "tool_calls": None}
        except Exception as e:
            if not self._using_mock:
                logger.info("Real LLM stream failed (%s), switching to mock", type(e).__name__)
                self._switch_to_mock()
                text = await self._backend.chat(messages, **kwargs)
                if text.startswith('{"tool_calls"'):
                    try:
                        parsed = json.loads(text)
                        yield {"content": None, "tool_calls": parsed.get("tool_calls", [])}
                    except json.JSONDecodeError:
                        for char in text:
                            yield {"content": char, "tool_calls": None}
                    return
                for char in text:
                    yield {"content": char, "tool_calls": None}
            else:
                raise

    async def _call_with_fallback(self, messages: list[dict], **kwargs) -> str:
        """Call LLM, falling back to mock on failure."""
        try:
            return await self._backend.chat(messages, **kwargs)
        except Exception as e:
            if not self._using_mock:
                logger.info("Real LLM unavailable (%s), using mock", type(e).__name__)
                self._switch_to_mock()
                return await self._backend.chat(messages, **kwargs)
            raise

    def _switch_to_mock(self) -> None:
        self._backend = MockLLMBackend()
        self._using_mock = True
        logger.info("Switched to mock LLM")

    async def close(self) -> None:
        if self._backend:
            await self._backend.close()

    @property
    def model(self) -> str:
        if self._using_mock:
            return "mock/elysia"
        return getattr(self._backend, "_model", "unknown")
