"""MessageArchive — 对话消息永久归档（MongoDB，只增不删）。"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.database.documentdb.protocol import DocumentDBBackend

logger = logging.getLogger(__name__)


class MessageArchive:
    """所有对话消息的永久归档存储。

    与 SessionMemory 职责分离:
    - SessionMemory: LLM 上下文窗口，截断保留最近 N 条
    - MessageArchive: 永久归档，只增不删，分页查询

    使用方式:
        archive = MessageArchive()
        await archive.connect(db_backend)       # 复用 session 的 MongoDB 连接
        await archive.append(msg)                # 每条消息 fire-and-forget
        messages, has_more = await archive.query(limit=50)  # 分页查询
    """

    COLLECTION = "message_archive"

    def __init__(self):
        self._db: "DocumentDBBackend | None" = None

    # ---- 连接 ----

    async def connect(self, db: "DocumentDBBackend") -> bool:
        """接入已有的 MongoDB backend。创建索引。返回是否成功。"""
        self._db = db
        # _id 字段 MongoDB 默认已有升序索引，倒序查询直接反向扫描，无需额外建索引
        logger.info("MessageArchive: connected, collection='%s'", self.COLLECTION)
        return True

    # ---- 写入 ----

    async def append(self, msg: dict) -> str:
        """追加一条消息。fire-and-forget — 调用方用 create_task 包装。返回 _id。"""
        if self._db is None:
            raise RuntimeError("MessageArchive not connected")
        _id = await self._db.insert_one(self.COLLECTION, msg)
        return _id

    # ---- 查询 ----

    async def query(
        self,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict], bool]:
        """分页查询消息，按 _id 降序（最新的在前）。

        before: 上一页最后一条的 _id（ObjectId 字符串），用于游标分页
        limit: 每页数量
        返回: (messages, has_more)
        """
        if self._db is None:
            return [], False

        filter_expr: dict = {}
        if before:
            from bson import ObjectId
            filter_expr["_id"] = {"$lt": ObjectId(before)}

        docs = await self._db.find(
            self.COLLECTION,
            filter_expr,
            sort=[("_id", -1)],
            limit=limit + 1,  # 多取一条判断 has_more
        )

        has_more = len(docs) > limit
        if has_more:
            docs = docs[:limit]

        return docs, has_more

    # ---- 管理 ----

    async def count(self) -> int:
        """返回归档消息总数。"""
        if self._db is None:
            return 0
        return await self._db.count(self.COLLECTION, {})

    async def clear_all(self) -> int:
        """清空全部归档。返回删除数量。"""
        if self._db is None:
            return 0
        deleted = await self._db.delete_many(self.COLLECTION, {})
        logger.info("MessageArchive: 清空 %d 条消息", deleted)
        return deleted
