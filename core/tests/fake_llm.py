"""Minimal LLM backend for tests — controllable responses, no API calls.

Provides:
- Token-level streaming (not char-by-char)
- Configurable fault injection (HTTP errors, timeouts, disconnects)
"""

from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

from Module.llm import LLMBackend

# Simple tokenization: split on word boundaries + punctuation for CJK-aware output
_TOKEN_RE = re.compile(r"([一-鿿]|[\w]+|[^\w\s])")  # noqa: RUF001


class FakeLLMBackend(LLMBackend):
    """Configurable fake LLM for testing. Returns whatever you set.

    Usage:
        # Normal: token-level streaming
        llm = FakeLLMBackend(response="你好，世界")

        # Fault injection:
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
        """Initialize fake backend.

        Args:
            response: Text to return from chat/chat_stream.
            fault: Optional injected fault: "http_429", "http_503",
                   "timeout", "disconnect".
            fault_on_call: Which call number triggers the fault (1-indexed).
        """
        self.response = response
        self.fault = fault
        self.fault_on_call = fault_on_call
        self._call_count = 0
        self._model = "fake/test"

    def _maybe_inject_fault(self) -> None:
        """Check and inject configured fault."""
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
            # Reset after first injection to allow subsequent calls to succeed
            self.fault = None

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Split text into token-like chunks (word/CJK-char level)."""
        tokens = _TOKEN_RE.findall(text)
        return tokens if tokens else list(text)

    async def chat(self, messages: list[dict], **kwargs) -> str:
        """Return canned response. Supports fault injection."""
        self._maybe_inject_fault()
        return self.response

    async def chat_stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """Yield response token by token (not char by char).
        Supports fault injection."""
        self._maybe_inject_fault()
        for token in self._tokenize(self.response):
            yield token

    async def test(self) -> dict:
        """Health check — always ok for fake backend."""
        return {"ok": True, "latency_ms": 0}

    async def close(self) -> None:
        """No-op close."""
        pass
