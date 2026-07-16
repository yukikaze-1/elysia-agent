"""MessageRepository — MongoDB message_archive 集合操作。"""

import logging

from bson import ObjectId
from pydantic import TypeAdapter

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from .base import Repository
from core.schema.types import MessageId, SessionId
from core.schema.message import Message, MessageRole

logger = logging.getLogger(__name__)

COLLECTION = "message_archive"

# TypeAdapter for Message union — cached at module level
_message_adapter: TypeAdapter[Message] = TypeAdapter(Message)


def _doc_to_message(doc: dict) -> Message:
    """将 MongoDB 文档转为 Message，_id 保留在 metadata._mongo_id 中。

    前端分页/删除需要 _id（MongoDB ObjectId）。Pydantic Message 模型不含 _id 字段，
    因此暂存在 metadata 中，由 MessageArchive 在序列化时提升为顶层字段。
    """
    msg = _message_adapter.validate_python(doc)
    if "_id" in doc:
        msg.metadata["_mongo_id"] = str(doc["_id"])
    return msg


class MessageRepository(Repository):
    """消息归档存储。"""

    def __init__(self, db: DocumentDBBackend | None = None):
        self._db: DocumentDBBackend | None = db

    # ---- lifecycle ----

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
            await self._db.ensure_index(COLLECTION, [("session_id", 1)])
            await self._db.ensure_index(COLLECTION, [("session_id", 1), ("_id", -1)])
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

    async def append(self, session_id: SessionId, msg: Message) -> None:
        """追加一条消息到永久归档。"""
        if self._db is None:
            return
        doc = msg.model_dump(mode="json")
        doc["session_id"] = session_id
        await self._db.insert_one(COLLECTION, doc)

    async def query_by_session(self, session_id: SessionId, *,
                               limit: int = 20, before: ObjectId | None = None,
                               skip: int = 0
                               ) -> tuple[list[Message], int]:
        """按 session 分页查询消息。返回 (消息列表, 总条数)。"""
        if self._db is None:
            return [], 0
        filter_expr: dict = {"session_id": session_id}
        if before:
            filter_expr["_id"] = {"$lt": before}
        sort = [("_id", -1)]
        docs = await self._db.find(COLLECTION, filter_expr, sort=sort, skip=skip, limit=limit)
        total = await self._db.count(COLLECTION, {"session_id": session_id})
        messages = [_doc_to_message(doc) for doc in docs]
        return messages, total

    async def query(self, *, before: ObjectId | str | None = None,
                    limit: int = 20) -> tuple[list[Message], bool]:
        """基于游标的分页查询（/api/messages 使用）。返回 (消息列表, has_more)。"""
        if self._db is None:
            return [], False
        filter_expr: dict = {}
        if before:
            oid = ObjectId(before) if isinstance(before, str) else before
            filter_expr["_id"] = {"$lt": oid}
        sort = [("_id", -1)]
        docs = await self._db.find(COLLECTION, filter_expr, sort=sort, limit=limit + 1)
        has_more = len(docs) > limit
        if has_more:
            docs = docs[:limit]
        messages = [_doc_to_message(doc) for doc in docs]
        return messages, has_more

    async def count_by_session(self, session_id: SessionId) -> int:
        if self._db is None:
            return 0
        return await self._db.count(COLLECTION, {"session_id": session_id})

    async def count(self) -> int:
        if self._db is None:
            return 0
        return await self._db.count(COLLECTION, {})

    async def delete_by_session(self, session_id: SessionId) -> int:
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {"session_id": session_id})

    async def clear_all(self) -> int:
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {})

    async def query_offset(self, *, skip: int = 0, limit: int = 50,
                           search: str = "", role: MessageRole | None = None
                           ) -> tuple[list[Message], int]:
        """offset 分页 + 搜索 + role 过滤。"""
        if self._db is None:
            return [], 0
        filter_expr: dict = {}
        if search:
            filter_expr["content"] = {"$elemMatch": {"content": {"$regex": search, "$options": "i"}}}
        if role is not None:
            filter_expr["role"] = role.value
        sort = [("_id", -1)]
        docs = await self._db.find(COLLECTION, filter_expr, sort=sort, skip=skip, limit=limit)
        total = await self._db.count(COLLECTION, filter_expr)
        messages = [_doc_to_message(doc) for doc in docs]
        return messages, total

    async def delete_one(self, msg_id: MessageId) -> int:
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {"_id": ObjectId(msg_id)})

    async def update_text(self, msg_id: MessageId, new_text: str) -> int:
        if self._db is None:
            return 0
        return await self._db.update_one(
            COLLECTION,
            {"_id": ObjectId(msg_id)},
            {"$set": {"content": [{"type": "text", "content": new_text}]}},
        )
