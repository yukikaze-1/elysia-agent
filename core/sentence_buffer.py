"""Sentence buffer — splits streaming tokens into complete sentences for TTS.

Accumulates LLM token output and yields complete sentences as they form,
ensuring GPT-SoVITS receives natural prosodic units rather than single chars.
"""

import re
from collections import deque
from typing import AsyncIterator


class SentenceBuffer:
    """Accumulates streaming text and yields full sentences."""

    # Split after these characters (include the delimiter in the sentence).
    SPLIT_PATTERN = re.compile(r"([。！？.!?\n])")

    def __init__(self, min_chunk_length: int = 2):
        self._buffer: str = ""
        self._min_chunk = min_chunk_length

    def feed(self, delta: str) -> str | None:
        """Feed a text delta. Returns a complete sentence if one is ready, else None."""
        self._buffer += delta
        return self._extract()

    def flush(self) -> str | None:
        """Flush remaining buffer content. Returns whatever is left, or None if empty."""
        if self._buffer.strip():
            remaining = self._buffer
            self._buffer = ""
            return remaining
        return None

    def _extract(self) -> str | None:
        parts = self.SPLIT_PATTERN.split(self._buffer)
        if len(parts) < 3:
            return None

        complete = parts[0] + parts[1]
        remaining = "".join(parts[2:])

        complete = complete.strip()
        if len(complete) < self._min_chunk:
            return None

        self._buffer = remaining
        return complete

    def reset(self) -> None:
        self._buffer = ""


class StreamingSentenceBuffer:
    """Async wrapper: consumes a token stream, yields complete sentences."""

    def __init__(self, min_chunk_length: int = 2):
        self._buffer = SentenceBuffer(min_chunk_length=min_chunk_length)

    async def consume(self, deltas: AsyncIterator[str]) -> AsyncIterator[str]:
        """Consume a token stream and yield complete sentences."""
        async for delta in deltas:
            sentence = self._buffer.feed(delta)
            if sentence:
                yield sentence
        remaining = self._buffer.flush()
        if remaining:
            yield remaining

    def reset(self) -> None:
        self._buffer.reset()
