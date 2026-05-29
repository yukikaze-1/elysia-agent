"""Minimal LLM backend for tests — controllable responses, no API calls."""

from typing import AsyncIterator

from Module.llm import LLMBackend


class FakeLLMBackend(LLMBackend):
    """Configurable fake LLM for testing. Returns whatever you set."""

    def __init__(self, response: str = "test response"):
        self.response = response
        self._model = "fake/test"

    async def chat(self, messages: list[dict], **kwargs) -> str:
        return self.response

    async def chat_stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        for char in self.response:
            yield char

    async def close(self) -> None:
        pass
