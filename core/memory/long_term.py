"""Long-term memory — in-memory fallback with optional Milvus + embedding.

Startup is fast: only Milvus connection is attempted. Embedding is loaded
lazily on first insert/search, with a short timeout to avoid blocking.
"""

import asyncio
import logging
import math
import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .embedding_provider import EmbeddingProvider

if TYPE_CHECKING:
    from core.storage.memory_repo import MemoryRepository

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    id: str = ""                    # 唯一标识（insert 时自动生成）
    text: str = ""
    embedding: list[float] = field(default_factory=list, repr=False)
    memory_type: str = "Note"      # Fact / Preference / Event / Plan / Note
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

    def __init__(self, max_entries: int = 10000, max_age_days: int = 90,
                 embed_timeout: float = 10.0,
                 enable_eviction: bool = True,
                 repo: "MemoryRepository | None" = None):
        self._entries: list[MemoryEntry] = []
        self._repo: "MemoryRepository | None" = repo
        self._embedding_provider = EmbeddingProvider(embed_timeout)
        self._dirty_ids: set[str] = set()  # 未同步到 Milvus 的条目 ID
        self._max_entries = max_entries
        self._max_age_days = max_age_days
        self._absorbed_ids: set[str] = set()  # 被 merge 吸收但尚未 delete 的条目 ID
        self._enable_eviction = enable_eviction
        self._write_lock = asyncio.Lock()     # 保护所有修改操作的并发锁

    async def connect(self) -> bool:
        """Connect to Milvus (fast). Embedding is deferred to first use. Returns True if Milvus available."""
        if self._repo is None:
            from core.storage.memory_repo import MemoryRepository
            self._repo = MemoryRepository()
        result = await self._repo.connect()
        if result:
            loaded = await self._load_from_milvus()
            if loaded:
                logger.info("LongTermMemory: loaded %d entries from Milvus", loaded)
            return True
        else:
            return False

    async def _load_from_milvus(self) -> int:
        """从 Milvus 恢复已有条目到内存。返回加载数量。"""
        if self._repo is None:
            return 0
        try:
            results = await self._repo.query(
                filter_expr="id >= 0",
                output_fields=["id", "entry_id", "text", "memory_type", "poignancy", "timestamp"],
                limit=self._max_entries,
            )
        except Exception as e:
            logger.warning("LongTermMemory: 从 Milvus 加载失败: %s", e)
            return 0

        from core.utils import new_message_id

        loaded = 0
        for row in results:
            pk = row.get("id")
            # 优先使用存储的 entry_id，兼容无 entry_id 字段的旧数据
            entry_id = row.get("entry_id", "")
            if not entry_id or not isinstance(entry_id, str):
                entry_id = new_message_id()
            entry = MemoryEntry(
                id=str(entry_id),
                text=row.get("text", ""),
                embedding=[],  # 不加载向量 — 太大，搜索走 Milvus
                memory_type=row.get("memory_type", "Note"),
                poignancy=int(row.get("poignancy", 5)),
                timestamp=float(row.get("timestamp", 0.0)),
                _milvus_pk=pk,
            )
            self._entries.append(entry)
            loaded += 1

        # 启动时驱逐超出上限的条目
        if loaded:
            await self._evict_overflow()

        return loaded

    async def health_check(self) -> bool:
        """尝试重连 Milvus。已连接则直接返回 True。"""
        if self._repo is not None:
            return await self._repo.health_check()
        return False

    # ---- Write ----

    async def insert(self, text: str, **metadata) -> None:
        """Insert a memory entry. Auto-embeds lazily if embedding is available.

        Keyword metadata may include: type (alias for memory_type), memory_type,
        poignancy, timestamp, source, etc.
        These become structured fields on MemoryEntry and are stored in Milvus.
        """
        embedding = await self._embedding_provider.embed(text)

        async with self._write_lock:
            from core.utils import new_message_id
            # Work on a copy to avoid mutating caller's dict
            meta = dict(metadata)
            # Accept 'type' as alias for 'memory_type'
            memory_type = meta.pop("memory_type", meta.pop("type", "Note"))
            entry = MemoryEntry(
                id=new_message_id(),
                text=text,
                embedding=embedding,
                memory_type=memory_type,
                poignancy=meta.pop("poignancy", 5),
                timestamp=meta.pop("timestamp", _time.time()),
                metadata=meta,
            )

            if self._repo is not None and embedding:
                try:
                    pk_list = await self._repo.insert(
                        vectors=[embedding],
                        metadata=[{
                            "entry_id": entry.id,
                            "text": text,
                            "memory_type": entry.memory_type,
                            "poignancy": entry.poignancy,
                            "timestamp": entry.timestamp,
                            **meta,
                        }],
                    )
                    if pk_list:
                        entry._milvus_pk = pk_list[0]
                        logger.debug("LongTermMemory insert: entry_id=%s, pk=%s", entry.id, pk_list[0])
                except Exception as e:
                    logger.warning("Milvus insert failed: %s, falling back to in-memory", e)
                    self._dirty_ids.add(entry.id)
            elif self._repo is None:
                self._dirty_ids.add(entry.id)

            self._entries.append(entry)
            if self._enable_eviction:
                await self._evict_overflow()

    async def _evict_overflow(self) -> int:
        """数量上限驱逐：超过 max_entries 时保留最新的 N 条。返回驱逐数量。

        在每次 insert() 后调用，保持内存列表 + Milvus 健康。
        """
        removed = 0
        if len(self._entries) <= self._max_entries:
            return removed

        # 按时间戳倒序，保留最新的 max_entries 条
        self._entries.sort(key=lambda e: e.timestamp, reverse=True)
        overflow = len(self._entries) - self._max_entries
        evicted = self._entries[-overflow:]
        evicted_ids = {e.id for e in evicted}
        evicted_pks = [e._milvus_pk for e in evicted if e._milvus_pk is not None]
        self._entries = self._entries[:self._max_entries]
        removed = overflow

        # 同步清理 Milvus
        if self._repo is not None and evicted_pks:
            try:
                await self._repo.delete(evicted_pks)
            except Exception:
                logger.debug("Milvus delete failed for evicted entries", exc_info=True)

        # 同步清理 dirty_ids
        if evicted_ids:
            self._dirty_ids -= evicted_ids

        if removed:
            logger.info("Long-term memory: 驱逐 %d 条超限条目 (剩余 %d)",
                       removed, len(self._entries))
        return removed

    def _get_by_id(self, entry_id: str) -> MemoryEntry | None:
        """通过 ID 查找条目。"""
        for e in self._entries:
            if e.id == entry_id:
                return e
        return None

    def get_all(self) -> list[MemoryEntry]:
        """返回全部活跃条目的副本（排除已吸收条目）。"""
        return [e for e in self._entries if e.id not in self._absorbed_ids]

    async def mark_absorbed(self, entry_id: str) -> None:
        """标记条目已被 merge 吸收，同步清理 Milvus。"""
        self._absorbed_ids.add(entry_id)
        async with self._write_lock:
            entry = self._get_by_id(entry_id)
            if entry is not None and entry._milvus_pk is not None and self._repo is not None:
                try:
                    await self._repo.delete([entry._milvus_pk])
                except Exception:
                    logger.debug("Milvus delete failed for absorbed entry %s",
                               entry_id, exc_info=True)

    async def _sync_dirty_entries(self) -> int:
        """尝试将脏条目同步到 Milvus。返回成功同步数。"""
        if self._repo is None or not self._dirty_ids:
            return 0
        synced = 0
        for eid in list(self._dirty_ids):
            entry = self._get_by_id(eid)
            if entry is None or not entry.embedding:
                continue
            try:
                pk_list = await self._repo.insert(
                    vectors=[entry.embedding],
                    metadata=[{
                        "entry_id": entry.id,
                        "text": entry.text,
                        "memory_type": entry.memory_type,
                        "poignancy": entry.poignancy,
                        "timestamp": entry.timestamp,
                        **entry.metadata,
                    }],
                )
                if pk_list:
                    entry._milvus_pk = pk_list[0]
                self._dirty_ids.discard(eid)
                synced += 1
            except Exception:
                break  # Milvus 仍然不可用
        if synced:
            logger.info("Synced %d dirty entries to Milvus", synced)
        return synced

    # ---- Delete ----

    async def update_text(
        self, entry_id: str, new_text: str,
        poignancy: int | None = None,
        memory_type: str | None = None,
    ) -> bool:
        """更新记忆文本（内存 + Milvus 重建）。返回 True 表示成功。"""
        async with self._write_lock:
            for i, e in enumerate(self._entries):
                if e.id == entry_id:
                    e.text = new_text
                    if poignancy is not None:
                        e.poignancy = poignancy
                    if memory_type is not None:
                        e.memory_type = memory_type
                    e.timestamp = _time.time()
                    if self._repo is not None and e._milvus_pk is not None:
                        old_pk = e._milvus_pk
                        emb = await self._embedding_provider.embed(new_text)
                        if emb:
                            try:
                                pk_list = await self._repo.insert(
                                    vectors=[emb],
                                    metadata=[{
                                        "entry_id": e.id,
                                        "text": new_text,
                                        "memory_type": e.memory_type,
                                        "poignancy": e.poignancy,
                                        "timestamp": e.timestamp,
                                    }],
                                )
                                if pk_list:
                                    e._milvus_pk = int(pk_list[0])
                                    logger.info("LongTermMemory update_text: entry_id=%s, old_pk=%s, new_pk=%s",
                                                entry_id, old_pk, pk_list[0])
                                    # 新向量插入成功后才删除旧向量
                                    try:
                                        await self._repo.delete([old_pk])
                                    except Exception:
                                        logger.debug("Milvus delete old pk failed for update %s", entry_id, exc_info=True)
                                else:
                                    # 插入返回空 → 标记脏，保留旧向量
                                    self._dirty_ids.add(entry_id)
                            except Exception:
                                logger.debug("Embedding/insert failed for update %s", entry_id, exc_info=True)
                                self._dirty_ids.add(entry_id)
                    return True
            return False

    async def delete(self, entry_id: str) -> bool:
        """删除单条记忆（内存 + Milvus）。返回 True 表示成功删除。"""
        async with self._write_lock:
            for i, e in enumerate(self._entries):
                if e.id == entry_id:
                    self._entries.pop(i)
                    self._absorbed_ids.discard(entry_id)
                    self._dirty_ids.discard(entry_id)
                    if self._repo is not None and e._milvus_pk is not None:
                        logger.info("LongTermMemory delete: entry_id=%s, milvus_pk=%s", entry_id, e._milvus_pk)
                        try:
                            await self._repo.delete([e._milvus_pk])
                        except Exception:
                            logger.debug("Milvus delete failed for %s", entry_id, exc_info=True)
                    return True
            return False

    async def clear_all(self) -> int:
        """清空所有记忆（内存 + Milvus 重建 collection）。可独立调用，无需先 connect()。"""
        async with self._write_lock:
            count = len(self._entries)
            logger.info("LongTermMemory: clearing all %d entries", count)
            self._entries.clear()
            self._absorbed_ids.clear()
            self._dirty_ids.clear()

            if self._repo is not None:
                # 已有 repo → 直接操作
                await self._repo.clear_all()
            else:
                # 无已有 repo → 自行创建临时 repo
                from core.storage.memory_repo import MemoryRepository
                try:
                    temp_repo = MemoryRepository()
                except Exception:
                    # MemoryRepository 创建失败（极端情况）→ 静默降级
                    return count
                try:
                    await temp_repo.connect()
                    await temp_repo.clear_all()
                except Exception:
                    # Milvus 不可达时静默降级：内存已清空，返回 count
                    pass
                finally:
                    await temp_repo.close()
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
        backend = await self._embedding_provider.ensure(wait=False)

        if not self._entries and self._repo is None:
            return []

        candidates: list[MemoryEntry] = []
        distances: dict[int, float | None] = {}  # id(entry) → L2 distance or None (fallback flag)

        # Try Milvus — fetch top_k * 4 for rerank candidate pool
        if self._repo is not None and await self._repo.health_check():
            vec = query_embedding
            if vec is None and query_text and backend is not None:
                try:
                    vec = await backend.embed_query(query_text)
                except Exception:
                    logger.debug("Embedding failed for search", exc_info=True)

            if vec:
                try:
                    raw = await self._repo.search(
                        vector=vec,
                        top_k=min(top_k * 4, 20),
                        output_fields=["id", "text", "memory_type", "poignancy", "timestamp"],
                    )
                    # Build PK→distance + text→distance (fallback) from Milvus results
                    pk_to_distance: dict[int, float] = {}
                    text_fb: dict[str, float] = {}
                    for r in raw:
                        pk = r.get("id")
                        text = r.get("text", "")
                        d = float(r.get("distance", r.get("score", 1.0)))
                        if pk is not None:
                            pk_to_distance[pk] = d
                        else:
                            text_fb[text] = d

                    # Match Milvus results against in-memory entries: PK first, text fallback
                    for e in self._entries:
                        if e._milvus_pk is not None and e._milvus_pk in pk_to_distance:
                            candidates.append(e)
                            distances[id(e)] = pk_to_distance[e._milvus_pk]
                        elif e._milvus_pk is None and e.text in text_fb:
                            candidates.append(e)
                            distances[id(e)] = text_fb[e.text]
                            logger.debug("LongTermMemory: entry matched via text fallback (no PK)")
                except Exception as e:
                    logger.warning("Milvus search failed: %s", e)

        # Exclude absorbed entries from search results
        candidates = [e for e in candidates if e.id not in self._absorbed_ids]

        # Fall back to in-memory if no candidates from Milvus
        fb_scores: dict[int, float] = {}
        if not candidates:
            fallback_results = self._search_fallback(
                query_text, min(top_k * 2, len(self._entries))
            )
            # Exclude absorbed entries from fallback results
            fallback_results = [(s, e) for s, e in fallback_results if e.id not in self._absorbed_ids]
            fb_scores = {id(e): s for s, e in fallback_results}
            for fb_score, entry in fallback_results:
                candidates.append(entry)
                distances[id(entry)] = None  # flag for rerank to use fb_score

        if not candidates:
            return []

        # Rerank: 0.5 × similarity + 0.3 × poignancy + 0.2 × recency
        now = _time.time()
        scored: list[tuple[float, MemoryEntry]] = []
        for entry in candidates:
            # Similarity: 1/(1+L2_distance) for Milvus, or fallback overlap score
            dist = distances.get(id(entry), 0.0)
            if dist is not None:
                sim = 1.0 / (1.0 + dist)
            else:
                sim = fb_scores.get(id(entry), 0.3)

            poi = min(entry.poignancy, 10) / 10.0  # normalize to [0,1]

            age_days = (now - entry.timestamp) / 86400 if entry.timestamp > 0 else 30
            rec = math.exp(-0.1 * age_days)  # exponential decay, half-life ~6.9 days

            score = 0.5 * sim + 0.3 * poi + 0.2 * rec
            scored.append((score, entry))

        # Filter by memory_type if requested
        # 注意：精确匹配，调用方需传 TitleCase（如 "Fact"）
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
        self._embedding_provider.cancel()
        if self._repo:
            await self._repo.close()
