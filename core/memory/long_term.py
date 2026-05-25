"""Long-term memory — in-memory fallback with optional Milvus + embedding.

When Milvus and an embedding backend are available, uses semantic search.
Otherwise falls back to character-overlap search (existing behavior).
"""

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.database.vectordb import VectorDBBackend
    from Module.embedding import EmbeddingBackend

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    text: str
    embedding: list[float] = field(default_factory=list, repr=False)
    metadata: dict = field(default_factory=dict)

    @property
    def dimension(self) -> int:
        return len(self.embedding)


class LongTermMemory:
    """Long-term memory with optional Milvus vector search and embedding.

    Graceful degradation:
    - Milvus available → stores vectors in Milvus, semantic ANN search
    - No Milvus → in-memory list + character-overlap search
    - Embedding available → auto-embed on insert for better search
    - No embedding → text-only storage, substring search
    """

    COLLECTION = "long_term_memory"
    DIMENSION = 1024  # Default for bge-large-zh-v1.5

    def __init__(self):
        self._entries: list[MemoryEntry] = []
        self._vector_db: "VectorDBBackend | None" = None
        self._embedding: "EmbeddingBackend | None" = None
        self._connected = False

    async def connect(self) -> bool:
        """Try to connect to Milvus and embedding. Returns True if vector search is available."""
        # Try embedding first (no external service needed for HuggingFace)
        try:
            from Module.embedding import EmbeddingFactory
            self._embedding = EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")
            logger.info("LongTermMemory: embedding backend connected (dim=%d)", self._embedding.dimension)
        except Exception as e:
            logger.info("LongTermMemory: embedding unavailable (%s), text-only mode", e)
            self._embedding = None

        # Try Milvus
        try:
            from Module.database.vectordb import VectorDBFactory
            self._vector_db = VectorDBFactory.create("milvus/memory")
            if not await self._vector_db.collection_exists(self.COLLECTION):
                dim = self._embedding.dimension if self._embedding else self.DIMENSION
                await self._vector_db.create_collection(self.COLLECTION, dimension=dim)
                logger.info("LongTermMemory: created Milvus collection '%s' (dim=%d)", self.COLLECTION, dim)
            self._connected = True
            logger.info("LongTermMemory: Milvus connected")
            return True
        except Exception as e:
            logger.info("LongTermMemory: Milvus unavailable (%s), using in-memory", e)
            self._vector_db = None
            self._connected = False
            return False

    # ---- Write ----

    async def insert(self, text: str, **metadata) -> None:
        """Insert a memory entry. Auto-embeds if embedding backend is available."""
        embedding: list[float] = []
        if self._embedding:
            try:
                embedding = await self._embedding.embed_query(text)
            except Exception:
                logger.debug("Embedding failed for insert, storing text-only", exc_info=True)

        entry = MemoryEntry(text=text, embedding=embedding, metadata=metadata)

        if self._vector_db and embedding:
            try:
                await self._vector_db.insert(
                    self.COLLECTION,
                    vectors=[embedding],
                    metadata=[{"text": text, **metadata}],
                )
            except Exception as e:
                logger.warning("Milvus insert failed: %s, falling back to in-memory", e)

        self._entries.append(entry)

    # ---- Read ----

    async def search(
        self,
        query_text: str = "",
        query_embedding: list[float] | None = None,
        top_k: int = 5,
    ) -> list[MemoryEntry]:
        """Search by embedding (if available) or text overlap (fallback)."""
        if not self._entries and not self._vector_db:
            return []

        # Try Milvus semantic search
        if self._vector_db and self._connected:
            vec = query_embedding
            if vec is None and query_text and self._embedding:
                try:
                    vec = await self._embedding.embed_query(query_text)
                except Exception:
                    logger.debug("Embedding failed for search", exc_info=True)

            if vec:
                try:
                    results = await self._vector_db.search(
                        self.COLLECTION,
                        vector=vec,
                        top_k=top_k,
                        output_fields=["text"],
                    )
                    return [
                        MemoryEntry(text=r.get("text", ""), metadata=r)
                        for r in results
                    ]
                except Exception as e:
                    logger.warning("Milvus search failed: %s", e)

        # Fall back to in-memory search
        return self._search_fallback(query_text, top_k)

    def _search_fallback(self, query: str, top_k: int) -> list[MemoryEntry]:
        if not query:
            return self._entries[-top_k:] if self._entries else []

        query_lower = query.lower()
        scored: list[tuple[int, MemoryEntry]] = []
        for e in self._entries:
            text_lower = e.text.lower()
            score = sum(1 for ch in query_lower if ch != " " and ch in text_lower)
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    # ---- Lifecycle ----

    @property
    def count(self) -> int:
        return len(self._entries)

    async def close(self) -> None:
        if self._vector_db:
            await self._vector_db.close()
        if self._embedding:
            await self._embedding.close()
