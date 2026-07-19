"""MemoryRepository — Milvus long_term_memory 集合操作。"""

import logging
from typing import TYPE_CHECKING

from Module.database.vectordb import VectorDBFactory, VectorDBBackend
from .base import Repository

if TYPE_CHECKING:
    from core.memory.long_term import MemoryEntry

logger = logging.getLogger(__name__)
COLLECTION = "long_term_memory"
DIMENSION = 1024


class MemoryRepository(Repository):
    """长期记忆存储 — Milvus 向量数据库。"""

    def __init__(self, db: VectorDBBackend | None = None):
        self._db: VectorDBBackend | None = db

    async def connect(self) -> bool:
        try:
            self._db = VectorDBFactory.create("milvus/memory")
            if not await self._db.collection_exists(COLLECTION):
                await self._db.create_collection(COLLECTION, DIMENSION)
            else:
                await self._db.load_collection(COLLECTION)
            logger.info("MemoryRepository: Milvus connected")
            return True
        except Exception as e:
            logger.warning("MemoryRepository: Milvus unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        if self._db is not None:
            return True
        try:
            self._db = VectorDBFactory.create("milvus/memory")
            logger.info("MemoryRepository: Milvus 已恢复连接")
            return True
        except Exception:
            return False

    async def insert(self, vectors: list[list[float]],
                     metadata: list[dict]) -> list[int]:
        """插入向量 + 元数据。返回主键列表。"""
        if self._db is None:
            return []
        return await self._db.insert(COLLECTION, vectors, metadata)

    async def search(self, vector: list[float], *,
                     top_k: int = 10, filter_expr: str | None = None,
                     output_fields: list[str] | None = None
                     ) -> list[dict]:
        """ANN 向量搜索。"""
        if self._db is None:
            return []
        return await self._db.search(
            COLLECTION, vector, top_k=top_k,
            filter_expr=filter_expr, output_fields=output_fields,
        )

    async def query(self, *, filter_expr: str | None = None,
                    output_fields: list[str] | None = None,
                    limit: int = 100) -> list[dict]:
        """标量查询（非向量搜索）。"""
        if self._db is None:
            return []
        return await self._db.query(
            COLLECTION, filter_expr=filter_expr,
            output_fields=output_fields, limit=limit,
        )

    async def delete(self, ids: list[int]) -> int:
        if self._db is None:
            return 0
        return await self._db.delete(COLLECTION, ids)

    async def count(self) -> int:
        """返回 Milvus 中条目数（上限 10000）。"""
        if self._db is None:
            return 0
        results = await self._db.query(
            COLLECTION, filter_expr="id >= 0", output_fields=["id"], limit=10000,
        )
        return len(results)

    async def clear_all(self) -> None:
        if self._db is None:
            return
        await self._db.drop_collection(COLLECTION)
        await self._db.create_collection(COLLECTION, DIMENSION)
