"""Long-term memory — in-memory fallback with optional Milvus + embedding.

Startup is fast: only Milvus connection is attempted. Embedding is loaded
lazily on first insert/search, with a short timeout to avoid blocking.
"""

import asyncio
import logging
import math
import os
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

    Embedding is loaded lazily on first use (not at connect time),
    with a 10s timeout to avoid blocking startup.
    """

    COLLECTION = "long_term_memory"
    DIMENSION = 1024  # Default for bge-large-zh-v1.5
    EMBED_TIMEOUT = 10.0  # Max seconds to wait for embedding model load

    def __init__(self):
        self._entries: list[MemoryEntry] = []
        self._vector_db: "VectorDBBackend | None" = None
        self._embedding: "EmbeddingBackend | None" = None
        self._embedding_tried = False  # Only try once lazily
        self._connected = False

    async def connect(self) -> bool:
        """Connect to Milvus (fast). Embedding is deferred to first use. Returns True if Milvus available."""
        # Try Milvus (fast — local connection, no model download)
        try:
            from Module.database.vectordb import VectorDBFactory
            self._vector_db = VectorDBFactory.create("milvus/memory")
            dim = self.DIMENSION
            if not await self._vector_db.collection_exists(self.COLLECTION):
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

    async def _ensure_embedding(self) -> None:
        """Load embedding backend lazily. Only tries once.

        Runs in a thread pool so the blocking model load doesn't stall the event loop.
        Does NOT block callers — if embedding isn't ready, callers use text-only fallback.
        """
        if self._embedding is not None or self._embedding_tried:
            return
        self._embedding_tried = True

        loop = asyncio.get_event_loop()

        async def _load():
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, self._try_load_embedding_sync),
                    timeout=self.EMBED_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.info("LongTermMemory: embedding load timed out after %.0fs, text-only mode", self.EMBED_TIMEOUT)
            except Exception as e:
                logger.info("LongTermMemory: embedding unavailable (%s), text-only mode", e)

        # Fire-and-forget: don't block callers waiting for embedding
        asyncio.create_task(_load(), name="embedding-load")

    def _try_load_embedding_sync(self) -> None:
        """Try OpenAI embedding first (fast), then HuggingFace (local). Runs in thread."""
        from Module.embedding import EmbeddingFactory

        # 1. Try OpenAI-compatible embedding (fast API call, no model download)
        openai_key = os.getenv("OPENAI_API_KEY", "")
        if openai_key:
            try:
                self._embedding = EmbeddingFactory.create("openai/text-embedding-3-small")
                dim = self._embedding.dimension
                logger.info("LongTermMemory: OpenAI embedding connected (dim=%d)", dim)
                return
            except Exception as e:
                logger.debug("OpenAI embedding unavailable: %s", e)

        # 2. Try HuggingFace local model (slower, loads model to memory)
        try:
            self._embedding = EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")
            dim = self._embedding.dimension
            logger.info("LongTermMemory: HuggingFace embedding connected (dim=%d)", dim)
        except Exception as e:
            logger.info("LongTermMemory: HuggingFace embedding unavailable: %s", e)
            self._embedding = None

    # ---- Write ----

    async def insert(self, text: str, **metadata) -> None:
        """Insert a memory entry. Auto-embeds lazily if embedding is available."""
        await self._ensure_embedding()

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
        await self._ensure_embedding()

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
