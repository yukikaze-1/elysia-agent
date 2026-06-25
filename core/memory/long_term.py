"""Long-term memory — in-memory fallback with optional Milvus + embedding.

Startup is fast: only Milvus connection is attempted. Embedding is loaded
lazily on first insert/search, with a short timeout to avoid blocking.
"""

import asyncio
import logging
import math
import os
import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.database.vectordb import VectorDBBackend
    from Module.embedding import EmbeddingBackend

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    id: str = ""                    # 唯一标识（insert 时自动生成）
    text: str = ""
    embedding: list[float] = field(default_factory=list, repr=False)
    memory_type: str = "note"      # Fact / Preference / Event / Plan / Note
    poignancy: int = 5             # 重要性 1-10
    timestamp: float = 0.0         # Unix 时间戳
    metadata: dict = field(default_factory=dict)
    _milvus_pk: int | None = field(default=None, repr=False, compare=False)

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

    def __init__(self, max_entries: int = 10000, max_age_days: int = 90,
                 embed_timeout: float = 10.0):
        self._entries: list[MemoryEntry] = []
        self._vector_db: "VectorDBBackend | None" = None
        self._embedding: "EmbeddingBackend | None" = None
        self._embedding_tried = False  # Only try once lazily
        self._embed_task: asyncio.Task | None = None  # Tracked for cleanup
        self._connected = False
        self._dirty_ids: set[str] = set()  # 未同步到 Milvus 的条目 ID
        self._max_entries = max_entries
        self._max_age_days = max_age_days
        self._embed_timeout = embed_timeout

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
            logger.warning("LongTermMemory: Milvus unavailable (%s), using in-memory", e)
            self._vector_db = None
            self._connected = False
            return False

    async def health_check(self) -> bool:
        """尝试重连 Milvus。已连接则直接返回 True。"""
        if self._connected:
            return True
        try:
            from Module.database.vectordb import VectorDBFactory
            self._vector_db = VectorDBFactory.create("milvus/memory")
            self._connected = True
            logger.info("LongTermMemory: Milvus 已恢复连接")
            return True
        except Exception:
            return False

    async def _ensure_embedding(self, wait: bool = False) -> None:
        """Load embedding backend lazily. Only tries once.

        Runs in a thread pool so the blocking model load doesn't stall the event loop.
        When wait=True, awaits the loading task so the caller gets embeddings.
        Otherwise fire-and-forget (search can fall back to text).
        """
        if self._embedding is not None:
            return
        if self._embedding_tried:
            if wait and self._embed_task and not self._embed_task.done():
                try:
                    await asyncio.wait_for(self._embed_task, timeout=self._embed_timeout)
                except (asyncio.TimeoutError, Exception):
                    pass
            return
        self._embedding_tried = True

        loop = asyncio.get_event_loop()

        async def _load():
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, self._try_load_embedding_sync),
                    timeout=self._embed_timeout,
                )
            except asyncio.TimeoutError:
                logger.info("LongTermMemory: embedding load timed out after %.0fs, text-only mode", self._embed_timeout)
            except Exception as e:
                logger.info("LongTermMemory: embedding unavailable (%s), text-only mode", e)

        self._embed_task = asyncio.create_task(_load(), name="embedding-load")

        if wait:
            try:
                await asyncio.wait_for(asyncio.shield(self._embed_task), timeout=self._embed_timeout)
            except (asyncio.TimeoutError, Exception):
                pass

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
        """Insert a memory entry. Auto-embeds lazily if embedding is available.

        Keyword metadata may include: type (alias for memory_type), memory_type,
        poignancy, timestamp, source, etc.
        These become structured fields on MemoryEntry and are stored in Milvus.
        """
        await self._ensure_embedding(wait=True)

        embedding: list[float] = []
        if self._embedding:
            try:
                embedding = await self._embedding.embed_query(text)
            except Exception:
                logger.debug("Embedding failed for insert, storing text-only", exc_info=True)

        from core.message_id import new_message_id
        # Work on a copy to avoid mutating caller's dict
        meta = dict(metadata)
        # Accept 'type' as alias for 'memory_type'
        memory_type = meta.pop("memory_type", meta.pop("type", "note"))
        entry = MemoryEntry(
            id=new_message_id(),
            text=text,
            embedding=embedding,
            memory_type=memory_type,
            poignancy=meta.pop("poignancy", 5),
            timestamp=meta.pop("timestamp", _time.time()),
            metadata=meta,
        )

        if self._vector_db and embedding:
            try:
                pk_list = await self._vector_db.insert(
                    self.COLLECTION,
                    vectors=[embedding],
                    metadata=[{
                        "text": text,
                        "memory_type": entry.memory_type,
                        "poignancy": entry.poignancy,
                        "timestamp": entry.timestamp,
                        **meta,
                    }],
                )
                if pk_list:
                    entry._milvus_pk = pk_list[0]
            except Exception as e:
                logger.warning("Milvus insert failed: %s, falling back to in-memory", e)
                self._dirty_ids.add(entry.id)
        elif not self._vector_db:
            self._dirty_ids.add(entry.id)

        self._entries.append(entry)
        self._evict_old_entries()

    def _evict_old_entries(self) -> int:
        """驱逐过期和超过上限的条目。返回驱逐数量。

        在每次 insert() 后调用，保持内存列表健康。
        """
        now = _time.time()
        cutoff = now - (self._max_age_days * 86400)
        removed = 0
        evicted_ids: set[str] = set()

        # 移除过期条目
        new_entries = []
        for e in self._entries:
            if e.timestamp >= cutoff:
                new_entries.append(e)
            else:
                evicted_ids.add(e.id)
                removed += 1
        self._entries = new_entries

        # 移除超过上限的条目（保留最新的）
        if len(self._entries) > self._max_entries:
            self._entries.sort(key=lambda e: e.timestamp, reverse=True)
            overflow = len(self._entries) - self._max_entries
            for e in self._entries[-overflow:]:
                evicted_ids.add(e.id)
            self._entries = self._entries[:self._max_entries]
            removed += overflow

        # 同步清理 dirty_ids
        if evicted_ids:
            self._dirty_ids -= evicted_ids

        if removed:
            logger.info("Long-term memory: 驱逐 %d 条过期/超限条目 (剩余 %d)",
                       removed, len(self._entries))
        return removed

    def _get_by_id(self, entry_id: str) -> MemoryEntry | None:
        """通过 ID 查找条目。"""
        for e in self._entries:
            if e.id == entry_id:
                return e
        return None

    async def _sync_dirty_entries(self) -> int:
        """尝试将脏条目同步到 Milvus。返回成功同步数。"""
        if not self._vector_db or not self._dirty_ids:
            return 0
        synced = 0
        for eid in list(self._dirty_ids):
            entry = self._get_by_id(eid)
            if entry is None or not entry.embedding:
                continue
            try:
                await self._vector_db.insert(
                    self.COLLECTION,
                    vectors=[entry.embedding],
                    metadata=[{
                        "text": entry.text,
                        "memory_type": entry.memory_type,
                        "poignancy": entry.poignancy,
                        "timestamp": entry.timestamp,
                        **entry.metadata,
                    }],
                )
                self._dirty_ids.discard(eid)
                synced += 1
            except Exception:
                break  # Milvus 仍然不可用
        if synced:
            logger.info("Synced %d dirty entries to Milvus", synced)
        return synced

    # ---- Delete ----

    async def delete(self, entry_id: str) -> bool:
        """删除单条记忆（内存 + Milvus）。返回 True 表示成功删除。"""
        for i, e in enumerate(self._entries):
            if e.id == entry_id:
                self._entries.pop(i)
                self._dirty_ids.discard(entry_id)
                if self._vector_db and e._milvus_pk is not None:
                    try:
                        await self._vector_db.delete(
                            self.COLLECTION, [e._milvus_pk])
                    except Exception:
                        logger.debug("Milvus delete failed for %s", entry_id, exc_info=True)
                return True
        return False

    async def clear_all(self) -> int:
        """清空所有记忆（内存 + Milvus 重建 collection）。返回删除数量。"""
        count = len(self._entries)
        self._entries.clear()
        self._dirty_ids.clear()
        if self._vector_db:
            await self._vector_db.drop_collection(self.COLLECTION)
            await self._vector_db.create_collection(
                self.COLLECTION, dimension=self.DIMENSION)
        return count

    # ---- Read ----

    async def search(
        self,
        query_text: str = "",
        query_embedding: list[float] | None = None,
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[MemoryEntry]:
        """向量搜索 + rerank（相似度 + 重要性 + 新鲜度）。"""
        await self._ensure_embedding()

        if not self._entries and not self._vector_db:
            return []

        candidates: list[MemoryEntry] = []
        distances: dict[str, float] = {}  # text → L2 distance (for rerank)

        # Try Milvus — fetch top_k * 4 for rerank candidate pool
        if self._vector_db and self._connected:
            vec = query_embedding
            if vec is None and query_text and self._embedding:
                try:
                    vec = await self._embedding.embed_query(query_text)
                except Exception:
                    logger.debug("Embedding failed for search", exc_info=True)

            if vec:
                try:
                    raw = await self._vector_db.search(
                        self.COLLECTION,
                        vector=vec,
                        top_k=min(top_k * 4, 20),
                        output_fields=["text", "memory_type", "poignancy", "timestamp"],
                    )
                    # Build text->distance lookup from Milvus results
                    milvus_texts: dict[str, float] = {}
                    for r in raw:
                        text = r.get("text", "")
                        milvus_texts[text] = float(r.get("distance", r.get("score", 1.0)))

                    # Match Milvus results against in-memory entries by text
                    # so returned entries have proper ids, embeddings, and metadata
                    for e in self._entries:
                        if e.text in milvus_texts:
                            candidates.append(e)
                            distances[e.text] = milvus_texts[e.text]
                except Exception as e:
                    logger.warning("Milvus search failed: %s", e)

        # Fall back to in-memory if no candidates from Milvus
        fb_scores: dict[str, float] = {}
        if not candidates:
            fallback_results = self._search_fallback(
                query_text, min(top_k * 2, len(self._entries))
            )
            fb_scores = {e.text: s for s, e in fallback_results}
            for fb_score, entry in fallback_results:
                candidates.append(entry)
                distances[entry.text] = None  # flag for rerank to use fb_score

        if not candidates:
            return []

        # Rerank: 0.5 × similarity + 0.3 × poignancy + 0.2 × recency
        now = _time.time()
        scored: list[tuple[float, MemoryEntry]] = []
        for entry in candidates:
            # Similarity: 1/(1+L2_distance) for Milvus, or fallback overlap score
            dist = distances.get(entry.text, 0.0)
            if dist is not None:
                sim = 1.0 / (1.0 + dist)
            else:
                sim = fb_scores.get(entry.text, 0.3)

            poi = min(entry.poignancy, 10) / 10.0  # normalize to [0,1]

            age_days = (now - entry.timestamp) / 86400 if entry.timestamp > 0 else 30
            rec = math.exp(-0.1 * age_days)  # exponential decay, half-life ~6.9 days

            score = 0.5 * sim + 0.3 * poi + 0.2 * rec
            scored.append((score, entry))

        # Filter by memory_type if requested
        if memory_type:
            scored = [(s, e) for s, e in scored if e.memory_type == memory_type]

        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    def _search_fallback(self, query: str, top_k: int) -> list[tuple[float, MemoryEntry]]:
        """In-memory text overlap search. Returns (score, entry) pairs for rerank."""
        if not query:
            return [(0.0, e) for e in self._entries[-top_k:]] if self._entries else []

        query_lower = query.lower()
        scored: list[tuple[float, MemoryEntry]] = []
        for e in self._entries:
            text_lower = e.text.lower()
            raw = sum(1 for ch in query_lower if ch != " " and ch in text_lower)
            if raw > 0:
                # Normalize overlap score relative to query length
                score = raw / max(len(query.strip()), 1)
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

    # ---- Lifecycle ----

    @property
    def count(self) -> int:
        return len(self._entries)

    async def close(self) -> None:
        # Cancel background embedding load if still running.
        # Note: the thread in run_in_executor can't be interrupted,
        # but the asyncio task is cleaned up. The thread will be
        # killed when the process exits.
        if self._embed_task and not self._embed_task.done():
            self._embed_task.cancel()
        if self._vector_db:
            await self._vector_db.close()
        # 仅解引用，模型释放由 EmbeddingFactory.shutdown() 统一负责
        if self._embedding:
            self._embedding = None
