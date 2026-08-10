"""IdempotencyRepository — MongoDB idempotency 集合（D6c 跨重启持久化）。

批次 2 起的 v2 幂等表是进程内 dict + TTL，重启丢失——ACK 丢失 + 后端重启后
Electron 重试可能重复生成。D6c 持久化到 Mongo：

文档 schema：
    {
        conversation_id,      # 目标会话
        client_request_id,    # 客户端请求 ID
        content_digest,       # 内容摘要（SHA-256 hex）
        message_id,           # 预分配用户消息 ID（ACK 与落库一致）
        status,               # in_progress / completed
        expires_at,           # BSON Date——TTL 索引 expireAfterSeconds=0 到期即删
    }

索引：
- {conversation_id, client_request_id} 唯一——并发同键双写被唯一索引兜底
  （claim 的 find→insert 非原子，但唯一索引保证不重复；后写者读回既有记录）。
- {expires_at} TTL（expireAfterSeconds=0）——BSON Date 真到期删除，不用字符串。

fail-closed 语义由调用方决定（store 产品模式 503 / dev 内存降级）；本 repo 在
Mongo 离线时一律 raise StorageUnavailableError（对齐附件 repo 先例）。
"""

from __future__ import annotations

import logging

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.storage.base import Repository, StorageUnavailableError

logger = logging.getLogger(__name__)

COLLECTION = "idempotency"

# 状态常量（幂等记录生命周期）
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"


def _is_duplicate_key(exc: BaseException) -> bool:
    """是否唯一索引冲突（并发同键双写）。驱动无关：检查 cause 链的异常类名。

    ``Repository._call`` 把后端异常包装为 ``StorageUnavailableError(reason) from e``，
    cause 即原始 driver 异常（pymongo DuplicateKeyError）。
    """
    cause = getattr(exc, "__cause__", None)
    return getattr(type(cause), "__name__", "") == "DuplicateKeyError"


class IdempotencyRepository(Repository):
    """幂等 claim 存储 — MongoDB idempotency 集合（D6c）。"""

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 database_name: str = "default",
                 base_url: str | None = None):
        """初始化幂等存储。

        Args:
            db: 可选的文档数据库后端，为 None 时需调用 connect() 建立连接。
            database_name: MongoDB 数据库名称，默认 "default"。
            base_url: MongoDB 连接串，为 None 时从 MONGODB_URI 环境变量读取。
        """
        self._db: DocumentDBBackend | None = db
        self._injected_db = db is not None
        self._database_name = database_name
        self._base_url = base_url

    def _require_db(self) -> DocumentDBBackend:
        """取后端；未连接（离线）→ StorageUnavailableError（fail-closed）。"""
        if self._db is None:
            raise StorageUnavailableError("IdempotencyRepository 未连接（MongoDB 不可用）")
        return self._db

    async def connect(self) -> bool:
        """建立连接并建索引（唯一 + TTL）。

        Returns:
            True 表示连接成功，False 表示失败。
        """
        if self._db is None:
            try:
                self._db = DocumentDBFactory.create(
                    f"mongodb/{self._database_name}",
                    base_url=self._base_url,
                )
            except Exception as e:
                logger.warning("IdempotencyRepository 建连失败: %s", e)
                self._db = None
                return False
        try:
            await self.ensure_indexes()
            logger.info("IdempotencyRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("IdempotencyRepository 索引创建失败: %s", e)
            if not self._injected_db:
                self._db = None
            return False

    async def ensure_indexes(self) -> None:
        """建唯一索引（防并发双写）+ TTL 索引（expires_at 到期即删）。

        create_index 幂等（已存在则 no-op）；connect 与测试均可直接调用。
        """
        db = self._db
        if db is None:
            return
        await db.ensure_index(
            COLLECTION,
            [("conversation_id", 1), ("client_request_id", 1)],
            unique=True,
        )
        await db.ensure_index(COLLECTION, [("expires_at", 1)], expireAfterSeconds=0)

    async def close(self) -> None:
        """关闭连接，释放资源。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        """真实健康探测（P1-05）。"""
        return await self._health_check()

    # ---- core operations ----

    async def claim(self, conversation_id: str, client_request_id: str, *,
                    content_digest: str, message_id: str, status: str,
                    expires_at: object) -> dict | None:
        """原子 claim。返回 None = 新 claim 成功；返回 dict = 已存在记录（并发/重放）。

        find → insert（唯一索引兜底并发）：并发同键双写时后写者收到 DuplicateKeyError，
        读回既有记录返回——不重复 generation（调用方用既有 message_id 重放 ACK）。

        Args:
            conversation_id: 目标会话 ID。
            client_request_id: 客户端请求 ID。
            content_digest: 内容摘要。
            message_id: 预分配用户消息 ID。
            status: 状态（in_progress / completed）。
            expires_at: BSON Date 过期时间（TTL 索引到期删除）。

        Returns:
            None 表示新 claim；dict 表示已存在记录。

        Raises:
            StorageUnavailableError: MongoDB 未连接/操作失败。
        """
        db = self._require_db()
        filter_expr = {
            "conversation_id": conversation_id,
            "client_request_id": client_request_id,
        }
        existing = await self._call(
            lambda: db.find(COLLECTION, filter_expr, limit=1),
            reason="IdempotencyRepository.claim 查询失败",
        )
        if existing:
            return existing[0]
        doc = {
            "conversation_id": conversation_id,
            "client_request_id": client_request_id,
            "content_digest": content_digest,
            "message_id": message_id,
            "status": status,
            "expires_at": expires_at,
        }
        try:
            await self._call(
                lambda: db.insert_one(COLLECTION, dict(doc)),
                reason="IdempotencyRepository.claim 写入失败",
            )
        except StorageUnavailableError as e:
            if not _is_duplicate_key(e):
                raise
            # 并发同键双写 → 读回既有记录（唯一索引保证单记录）
            existing = await self._call(
                lambda: db.find(COLLECTION, filter_expr, limit=1),
                reason="IdempotencyRepository.claim 冲突读回失败",
            )
            if existing:
                return existing[0]
            raise  # 极罕见：索引冲突但记录已 TTL 删除 → fail-closed
        return None

    async def get(self, conversation_id: str, client_request_id: str) -> dict | None:
        """按 (conversation_id, client_request_id) 取单条记录；不存在返回 None。"""
        db = self._require_db()
        results = await self._call(
            lambda: db.find(
                COLLECTION,
                {"conversation_id": conversation_id,
                 "client_request_id": client_request_id},
                limit=1,
            ),
            reason="IdempotencyRepository.get 查询失败",
        )
        return results[0] if results else None

    async def complete(self, conversation_id: str, client_request_id: str) -> None:
        """标记 claim 已完成（accepted ACK 后调用；幂等）。"""
        db = self._require_db()
        await self._call(
            lambda: db.update_one(
                COLLECTION,
                {"conversation_id": conversation_id,
                 "client_request_id": client_request_id},
                {"$set": {"status": STATUS_COMPLETED}},
            ),
            reason="IdempotencyRepository.complete 写入失败",
        )

    async def release(self, conversation_id: str, client_request_id: str) -> None:
        """释放 claim（消息被拒/异常后删除，允许重试重新进入）。"""
        db = self._require_db()
        await self._call(
            lambda: db.delete_many(
                COLLECTION,
                {"conversation_id": conversation_id,
                 "client_request_id": client_request_id},
            ),
            reason="IdempotencyRepository.release 删除失败",
        )
