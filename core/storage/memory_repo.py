"""MemoryRepository — Milvus long_term_memory 集合操作。"""

import logging
from typing import TYPE_CHECKING

from Module.database.vectordb import VectorDBFactory, VectorDBBackend
from core.storage.base import Repository, StorageUnavailableError
from core.storage.destructive_guard import require_confirm

if TYPE_CHECKING:
    from core.memory.long_term import MemoryEntry

logger = logging.getLogger(__name__)
COLLECTION = "long_term_memory"
CONFIRM_TOKEN = "ltm"
"""破坏性 `clear_all` 的确认令牌（稳定资源名——理由见 core/storage/destructive_guard.py）。"""
DIMENSION = 1024  # 模块级保留（兼容外部 import）；权威值见 MemoryRepository.DIMENSION


class MemoryRepository(Repository):
    """长期记忆存储 — Milvus 向量数据库。"""

    # collection 名（实例级——v2 子类 MemoryRepositoryV2 遮蔽为 long_term_memory_v2；
    # 模块级 COLLECTION 保留兼容外部 import，但方法内一律 self.COLLECTION 以支持子类遮蔽）
    COLLECTION = "long_term_memory"
    # 向量维度（实例级同源——方法内用 self.DIMENSION；调用方可据此对齐 embedding 维度，
    # 避免 embedding provider 输出维度与集合固定维度不一致导致写入被拒）
    DIMENSION = DIMENSION
    # 破坏性 clear_all 的确认令牌（**稳定资源名**，与 COLLECTION 解耦：v1/v2 子类共用，
    # 且不受测试防线重写 COLLECTION 影响——理由见 core/storage/destructive_guard.py）
    CONFIRM_TOKEN = CONFIRM_TOKEN

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
        self._injected_db = db is not None

    async def connect(self) -> bool:
        """建立 Milvus 连接，创建或加载 collection。

        Returns:
            True 表示连接成功，False 表示连接失败。
        """
        # S2-B5：全部 Mongo 仓储都有这道守卫，唯独本仓储此前没有——于是
        # `_health_check` 里 `_mark_unavailable()` 之后的 `connect()` 会**无条件
        # 新建客户端**，与"已连接就别再建"的意图相悖。已连接时先探活，活着就复用。
        if self._db is not None and not self._injected_db:
            try:
                if await self._db.collection_exists(self.COLLECTION):
                    return True
            except Exception:
                pass
            # 探活失败：走下面的重建路径（旧引用已由 _mark_unavailable 或本处释放）
        try:
            self._db = VectorDBFactory.create(
                "milvus/memory",
                base_url=self._base_url,
                token=self._token,
            )
            if not await self._db.collection_exists(self.COLLECTION):
                await self._db.create_collection(self.COLLECTION, self.DIMENSION)
            else:
                await self._db.load_collection(self.COLLECTION)
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
        """健康检查：真实 ping（P1-05），失败时重连。

        Returns:
            True 表示可用，False 表示不可用。
        """
        return await self._health_check()

    def _require_db(self) -> VectorDBBackend:
        """fail-closed：未连接（离线）时 raise `StorageUnavailableError`。

        与同仓 `clear_all` 同一约定（#31 写路径收口 4–5/5）：离线**不得**用 `[]` / `0`
        冒充业务结果——调用方分不清「离线」与「没有条目」，这正是"静默丢写"的形状。
        降级语义由调用方显式承载（`LongTermMemory` 的两个收口点转成 dirty + journal）。

        Raises:
            StorageUnavailableError: 向量库未连接。
        """
        if self._db is None:
            raise StorageUnavailableError(
                f"MemoryRepository({self.COLLECTION}) 未连接：拒绝用空结果冒充成功")
        return self._db

    async def insert(self, vectors: list[list[float]],
                     metadata: list[dict]) -> list[int]:
        """插入向量 + 元数据。

        Args:
            vectors: 向量列表，每个向量为 float 列表。
            metadata: 元数据列表，与 vectors 一一对应。

        Returns:
            插入成功的主键列表（**空列表 = 后端返回了空 PK**，不再表示"离线"）。

        Raises:
            StorageUnavailableError: 向量库未连接（离线）——调用方决定降级语义
                （`LongTermMemory` 记 dirty + 落 journal 待补写，不是丢写）。
        """
        db = self._require_db()
        return await db.insert(self.COLLECTION, vectors, metadata)

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
            self.COLLECTION, vector, top_k=top_k, **kwargs,
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
        return await self._db.query(self.COLLECTION, **kwargs)

    async def delete(self, ids: list[int]) -> int:
        """按主键删除记忆。

        Args:
            ids: 要删除的主键列表。

        Returns:
            实际删除的条数（**0 = 后端没删到行**，不再表示"离线"）。

        Raises:
            StorageUnavailableError: 向量库未连接（离线）——调用方记 tombstone 待重连清理
                （`LongTermMemory` 的四处删除调用点都据此防"旧行重启复活"）。
        """
        db = self._require_db()
        result = await db.delete(self.COLLECTION, ids)
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
            self.COLLECTION, filter_expr="id >= 0", output_fields=["id"], limit=10000,
        )
        return len(results)

    async def clear_all(self, *, confirm: str) -> None:
        """清空所有记忆，删除并重建 collection。

        **破坏性且不可逆**（`drop + recreate`），因此要求类型化确认。
        此前 `_db is None` 时静默 `return`——调用方会误以为已清空，现改 fail-closed。

        Args:
            confirm: 必须等于 `self.CONFIRM_TOKEN`（破坏性操作的目标资源确认）。

        Raises:
            DestructiveConfirmRequired: confirm 与 `self.CONFIRM_TOKEN` 不一致。
            StorageUnavailableError: 向量库未连接。
        """
        require_confirm(
            target=self.CONFIRM_TOKEN, confirm=confirm, operation="清空长期记忆",
        )
        db = self._require_db()
        logger.warning("MemoryRepository clear_all: dropping + recreating collection %s", self.COLLECTION)
        await db.drop_collection(self.COLLECTION)
        await db.create_collection(self.COLLECTION, self.DIMENSION)
