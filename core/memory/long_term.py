"""Long-term memory — in-memory vector store fallback.

In production this delegates to Milvus via Module.database.vectordb.
For the demo we keep an in-memory list with cosine-similarity search.
"""

import math
from dataclasses import dataclass, field


@dataclass
class MemoryEntry:
    text: str
    embedding: list[float] = field(default_factory=list, repr=False)
    metadata: dict = field(default_factory=dict)

    @property
    def dimension(self) -> int:
        return len(self.embedding)


class LongTermMemory:
    """In-memory long-term memory store with cosine-similarity search."""

    def __init__(self):
        self._entries: list[MemoryEntry] = []

    def insert(self, text: str, embedding: list[float] | None = None, **metadata) -> None:
        """Insert a memory entry. If no embedding, stores text-only (text-match fallback)."""
        self._entries.append(MemoryEntry(
            text=text,
            embedding=embedding or [],
            metadata=metadata,
        ))

    def search(self, query_embedding: list[float] | None = None, query_text: str = "", top_k: int = 5) -> list[MemoryEntry]:
        """Search by embedding (cosine similarity) or by text substring when no embeddings."""
        if not self._entries:
            return []

        if query_embedding and self._entries and self._entries[0].embedding:
            return self._search_embedding(query_embedding, top_k)
        else:
            return self._search_text(query_text, top_k)

    def _search_embedding(self, query: list[float], top_k: int) -> list[MemoryEntry]:
        scored = [(self._cosine(query, e.embedding), e) for e in self._entries]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    def _search_text(self, query: str, top_k: int) -> list[MemoryEntry]:
        """Simple keyword-overlap search that works for both Chinese and English."""
        query_lower = query.lower()
        scored: list[tuple[int, MemoryEntry]] = []
        for e in self._entries:
            text_lower = e.text.lower()
            # Score: count how many query characters appear in the text
            score = sum(1 for ch in query_lower if ch != " " and ch in text_lower)
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @property
    def count(self) -> int:
        return len(self._entries)
