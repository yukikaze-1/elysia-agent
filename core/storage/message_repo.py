"""MessageRepository — MongoDB message_archive 集合操作。"""

import logging
from typing import TYPE_CHECKING

from bson import ObjectId

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from .base import Repository

if TYPE_CHECKING:
    from core.schema.message import Message

logger = logging.getLogger(__name__)

COLLECTION = "message_archive"


class MessageRepository(Repository):
    """消息归档存储。"""

    def __init__(self, db: DocumentDBBackend | None = None):
        self._db: DocumentDBBackend | None = db

    # ---- lifecycle ----

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
            await self._db.ensure_index(COLLECTION, [("session_id", 1)])
            logger.info("MessageRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("MessageRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return self._db is not None

    # ---- core operations ----

    async def append(self, session_id: str, msg: dict) -> None:
        """追加一条消息到永久归档。"""
        if self._db is None:
            return
        msg["session_id"] = session_id
        await self._db.insert_one(COLLECTION, msg)

    async def query_by_session(self, session_id: str, *,
                               limit: int = 20, before: ObjectId | None = None
                               ) -> tuple[list[dict], int]:
        """按 session 分页查询消息。返回 (消息列表, 总条数)。"""
        if self._db is None:
            return [], 0
        filter_expr: dict = {"session_id": session_id}
        if before:
            filter_expr["_id"] = {"$lt": before}
        sort = [("_id", -1)]
        messages = await self._db.find(COLLECTION, filter_expr, sort=sort, limit=limit)
        total = await self._db.count(COLLECTION, {"session_id": session_id})
        return messages, total

    async def count_by_session(self, session_id: str) -> int:
        if self._db is None:
            return 0
        return await self._db.count(COLLECTION, {"session_id": session_id})

    async def count(self) -> int:
        if self._db is None:
            return 0
        return await self._db.count(COLLECTION, {})

    async def delete_by_session(self, session_id: str) -> int:
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {"session_id": session_id})

    async def clear_all(self) -> int:
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {})

    async def query_offset(self, *, skip: int = 0, limit: int = 50,
                           search: str = "", role: str = "") -> tuple[list[dict], int]:
        """offset 分页 + 搜索 + role 过滤。"""
        if self._db is None:
            return [], 0
        filter_expr: dict = {}
        if search:
            filter_expr["content"] = {"$elemMatch": {"content": {"$regex": search, "$options": "i"}}}
        if role:
            filter_expr["role"] = role
        sort = [("_id", -1)]
        messages = await self._db.find(COLLECTION, filter_expr, sort=sort, skip=skip, limit=limit)
        total = await self._db.count(COLLECTION, filter_expr)
        return messages, total

    async def delete_one(self, msg_id: str) -> int:
        if self._db is None:
            return 0
        from bson import ObjectId as ObjId
        return await self._db.delete_many(COLLECTION, {"_id": ObjId(msg_id)})

    async def update_text(self, msg_id: str, new_text: str) -> int:
        if self._db is None:
            return 0
        from bson import ObjectId as ObjId
        return await self._db.update_one(
            COLLECTION,
            {"_id": ObjId(msg_id)},
            {"$set": {"content": [{"type": "text", "content": new_text}]}},
        )
