"""MemoryRepositoryV2 — 面向 long_term_memory_v2 的 Milvus 仓储（架构稿 §8.3）。"""
from __future__ import annotations

from core.storage.memory_repo import MemoryRepository


class MemoryRepositoryV2(MemoryRepository):
    """MemoryRepository 的 v2 变体：COLLECTION 指向 long_term_memory_v2。

    复用父类 Milvus 操作（insert/search/query/delete/count/clear_all），
    仅 COLLECTION 不同——产品记忆全部写 v2（scope 字段：user_id/character_instance_id/visibility）。
    """
    COLLECTION = "long_term_memory_v2"
