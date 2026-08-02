"""MessageRepository — MongoDB message_archive 集合操作。"""

import logging

from bson import ObjectId

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from core.storage.base import Repository
from core.schema.adapters.message_adapter import validate_message_legacy_compat
from core.schema.types import MessageId, SessionId
from core.schema.message import Message, MessageRole

logger = logging.getLogger(__name__)

COLLECTION = "message_archive"


def _doc_to_message(doc: dict) -> Message:
    """将 MongoDB 文档转为 Message。

    _id 保留在 metadata._mongo_id 中。前端分页/删除需要 _id（MongoDB ObjectId）。
    Pydantic Message 模型不含 _id 字段，因此暂存在 metadata 中，
    由调用方（server.py）在序列化时提升为顶层字段。

    Args:
        doc: MongoDB 文档。

    Returns:
        转换后的 Message 对象。
    """
    msg = validate_message_legacy_compat(doc)
    if "_id" in doc:
        msg.metadata["_mongo_id"] = str(doc["_id"])
    return msg


class MessageRepository(Repository):
    """消息归档存储。"""

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 database_name: str = "default",
                 base_url: str | None = None):
        """初始化消息归档存储。

        Args:
            db: 可选的文档数据库后端，为 None 时需调用 connect() 建立连接。
            database_name: MongoDB 数据库名称，默认 "default"。
            base_url: MongoDB 连接串，为 None 时从 MONGODB_URI 环境变量读取。
        """
        self._db: DocumentDBBackend | None = db
        self._database_name = database_name
        self._base_url = base_url

    # ---- lifecycle ----

    async def connect(self) -> bool:
        """建立 MongoDB 连接，创建索引。

        Returns:
            True 表示连接成功，False 表示连接失败。
        """
        try:
            self._db = DocumentDBFactory.create(
                f"mongodb/{self._database_name}",
                base_url=self._base_url,
            )
            await self._db.ensure_index(COLLECTION, [("session_id", 1)])
            await self._db.ensure_index(COLLECTION, [("session_id", 1), ("_id", -1)])
            logger.info("MessageRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("MessageRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        """关闭数据库连接，释放资源。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        """健康检查。

        Returns:
            True 表示可用，False 表示不可用。
        """
        return self._db is not None

    # ---- core operations ----

    async def append(self, session_id: SessionId, msg: Message) -> None:
        """追加一条消息到永久归档。

        Args:
            session_id: 会话 ID。
            msg: 要归档的消息对象。
        """
        if self._db is None:
            return
        doc = msg.model_dump(mode="json")
        doc["session_id"] = session_id
        await self._db.insert_one(COLLECTION, doc)

    async def query_by_session(self, session_id: SessionId, *,
                               limit: int = 20, before: ObjectId | None = None,
                               skip: int = 0
                               ) -> tuple[list[Message], int]:
        """按 session 分页查询消息。

        Args:
            session_id: 会话 ID。
            limit: 返回条数上限，默认 20。
            before: 游标，只返回此 ObjectId 之前的消息。
            skip: 跳过的记录数，默认 0。

        Returns:
            (消息列表, 总条数) 元组。若数据库不可用返回 ([], 0)。
        """
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
        """基于游标的分页查询（/api/messages 使用）。

        Args:
            before: 游标，只返回此 ObjectId 之前的消息。
            limit: 每页条数，默认 20。

        Returns:
            (消息列表, has_more) 元组。若数据库不可用返回 ([], False)。
        """
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
        """按会话统计消息数。

        Args:
            session_id: 会话 ID。

        Returns:
            消息总数。若数据库不可用返回 0。
        """
        if self._db is None:
            return 0
        return await self._db.count(COLLECTION, {"session_id": session_id})

    async def count(self) -> int:
        """返回全部消息总数。

        Returns:
            消息总数。若数据库不可用返回 0。
        """
        if self._db is None:
            return 0
        return await self._db.count(COLLECTION, {})

    async def delete_by_session(self, session_id: SessionId) -> int:
        """按会话删除所有消息。

        Args:
            session_id: 会话 ID。

        Returns:
            删除的消息数。若数据库不可用返回 0。
        """
        if self._db is None:
            return 0
        result = await self._db.delete_many(COLLECTION, {"session_id": session_id})
        logger.info("MessageRepository delete_by_session: session_id=%s, deleted=%d", session_id, result)
        return result

    async def clear_all(self) -> int:
        """清空全部消息归档。

        Returns:
            删除的消息数。若数据库不可用返回 0。
        """
        if self._db is None:
            return 0
        result = await self._db.delete_many(COLLECTION, {})
        logger.warning("MessageRepository clear_all: deleted=%d messages", result)
        return result

    async def query_offset(self, *, skip: int = 0, limit: int = 50,
                           search: str = "", role: MessageRole | None = None
                           ) -> tuple[list[Message], int]:
        """offset 分页查询，支持搜索和角色过滤。

        Args:
            skip: 跳过的记录数，默认 0。
            limit: 每页条数，默认 50。
            search: 搜索关键词，对消息内容做大小写不敏感正则匹配。
            role: 角色过滤，为 None 时不过滤。

        Returns:
            (消息列表, 总条数) 元组。若数据库不可用返回 ([], 0)。
        """
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
        """按 ID 删除单条消息。

        Args:
            msg_id: 消息 ID。

        Returns:
            删除的消息数。若数据库不可用返回 0。
        """
        if self._db is None:
            return 0
        result = await self._db.delete_many(COLLECTION, {"_id": ObjectId(msg_id)})
        logger.info("MessageRepository delete_one: msg_id=%s, deleted=%d", msg_id, result)
        return result

    async def update_text(self, msg_id: MessageId, new_text: str) -> int:
        """更新消息文本内容。

        Args:
            msg_id: 消息 ID。
            new_text: 新的文本内容。

        Returns:
            修改的记录数。若数据库不可用返回 0。
        """
        if self._db is None:
            return 0
        result = await self._db.update_one(
            COLLECTION,
            {"_id": ObjectId(msg_id)},
            {"$set": {"content": [{"type": "text", "content": new_text}]}},
        )
        logger.info("MessageRepository update_text: msg_id=%s, text_len=%d, modified=%d", msg_id, len(new_text), result)
        return result
