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

    def __init__(self, db: VectorDBBackend | None = None, *,
                 base_url: str | None = None,
                 token: str | None = None):
        """初始化长期记忆存储。

        Args:
            db: 可选的向量数据库后端，为 None 时需调用 connect() 建立连接。
            base_url: Milvus 服务端点，为 None 时从 MILVUS_URI 环境变量读取。
            token: Milvus 认证令牌，为 None 时从 MILVUS_TOKEN 环境变量读取。
        """
        self._db: VectorDBBackend | None = db
        self._base_url = base_url
        self._token = token

    async def connect(self) -> bool:
        """建立 Milvus 连接，创建或加载 collection。

        Returns:
            True 表示连接成功，False 表示连接失败。
        """
        try:
            self._db = VectorDBFactory.create(
                "milvus/memory",
                base_url=self._base_url,
                token=self._token,
            )
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
        """关闭 Milvus 连接，释放资源。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        """健康检查。若连接断开则尝试重连。

        Returns:
            True 表示可用，False 表示不可用。
        """
        if self._db is not None:
            return True
        try:
            self._db = VectorDBFactory.create(
                "milvus/memory",
                base_url=self._base_url,
                token=self._token,
            )
            logger.info("MemoryRepository: Milvus 已恢复连接")
            return True
        except Exception:
            return False

    async def insert(self, vectors: list[list[float]],
                     metadata: list[dict]) -> list[int]:
        """插入向量 + 元数据。

        Args:
            vectors: 向量列表，每个向量为 float 列表。
            metadata: 元数据列表，与 vectors 一一对应。

        Returns:
            插入成功的主键列表。若数据库不可用返回空列表。
        """
        if self._db is None:
            return []
        return await self._db.insert(COLLECTION, vectors, metadata)

    async def search(self, vector: list[float], *,
                     top_k: int = 10, filter_expr: str | None = None,
                     output_fields: list[str] | None = None
                     ) -> list[dict]:
        """ANN 向量搜索。

        Args:
            vector: 查询向量。
            top_k: 返回结果数量上限，默认 10。
            filter_expr: 可选的标量过滤表达式。
            output_fields: 需要返回的附加字段列表。

        Returns:
            搜索结果列表，每项为 dict。若数据库不可用返回空列表。
        """
        if self._db is None:
            return []
        kwargs: dict = {"output_fields": output_fields}
        if filter_expr is not None:
            kwargs["filter_expr"] = filter_expr
        return await self._db.search(
            COLLECTION, vector, top_k=top_k, **kwargs,
        )

    async def query(self, *, filter_expr: str | None = None,
                    output_fields: list[str] | None = None,
                    limit: int = 100) -> list[dict]:
        """标量查询（非向量搜索）。

        Args:
            filter_expr: 可选的过滤表达式。
            output_fields: 需要返回的字段列表。
            limit: 返回结果数量上限，默认 100。

        Returns:
            查询结果列表。若数据库不可用返回空列表。
        """
        if self._db is None:
            return []
        kwargs: dict = {"output_fields": output_fields, "limit": limit}
        if filter_expr is not None:
            kwargs["filter_expr"] = filter_expr
        return await self._db.query(COLLECTION, **kwargs)

    async def delete(self, ids: list[int]) -> int:
        """按主键删除记忆。

        Args:
            ids: 要删除的主键列表。

        Returns:
            实际删除的条数。若数据库不可用返回 0。
        """
        if self._db is None:
            return 0
        result = await self._db.delete(COLLECTION, ids)
        logger.info("MemoryRepository delete: ids=%s, count=%d", ids[:5], result)
        return result

    async def count(self) -> int:
        """返回 Milvus 中条目数。

        Returns:
            条目数（上限 10000）。若数据库不可用返回 0。
        """
        if self._db is None:
            return 0
        results = await self._db.query(
            COLLECTION, filter_expr="id >= 0", output_fields=["id"], limit=10000,
        )
        return len(results)

    async def clear_all(self) -> None:
        """清空所有记忆，删除并重建 collection。"""
        if self._db is None:
            return
        logger.warning("MemoryRepository clear_all: dropping + recreating collection %s", COLLECTION)
        await self._db.drop_collection(COLLECTION)
        await self._db.create_collection(COLLECTION, DIMENSION)
