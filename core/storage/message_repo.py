"""MessageRepository — MongoDB message_archive 集合操作。"""

import base64
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bson import ObjectId

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from core.storage.base import Repository
from core.schema.adapters.message_adapter import validate_message_legacy_compat
from core.schema.types import MessageId, SessionId
from core.schema.message import Message, MessageRole

logger = logging.getLogger(__name__)

COLLECTION = "message_archive"

# query_search 支持的 category 取值 → content part 类型 / role 映射。
# link 特殊：archive 无独立 link part，链接在 TextPart 内，category=link 走 URL 内容正则特判。
_CATEGORY_PART_TYPES = {"text", "image", "link", "video", "audio", "file"}


def _oid(value: str) -> object:
    """将字符串 _id 转 ObjectId（无法转换时原样返回）。"""
    try:
        return ObjectId(value)
    except Exception:
        return value


def _encode_cursor(timestamp: str, doc_id: str) -> str:
    """编码 keyset 游标：base64(`timestamp|_id`)。"""
    raw = f"{timestamp}|{doc_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """解码 keyset 游标 → (timestamp, _id)。格式非法抛 ValueError。"""
    raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    timestamp, _, doc_id = raw.partition("|")
    if not timestamp or not doc_id:
        raise ValueError("游标格式非法")
    return timestamp, doc_id


def _build_date_bounds(date_from: str | None, date_to: str | None,
                       tz: ZoneInfo) -> tuple[str | None, str | None]:
    """把自然日区间转换为半开 UTC 时间戳区间。

    date_from 含当天（dayStart）；date_to 含当天（nextDayStart 排他）。
    仅 date_from → [dayStart, +inf)；仅 date_to → (-inf, nextDayStart)。
    两个都缺失返回 (None, None)。非法日期格式抛 ValueError（由调用方兜底）。

    Args:
        date_from: 起始自然日 YYYY-MM-DD。
        date_to: 结束自然日 YYYY-MM-DD。
        tz: IANA 时区。

    Returns:
        (UTC 起始 ISO 字符串 | None, UTC 结束 ISO 字符串 | None)。
    """
    start: str | None = None
    end: str | None = None
    if date_from:
        d = date.fromisoformat(date_from)
        start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc).isoformat()
    if date_to:
        d = date.fromisoformat(date_to)
        next_day = datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(days=1)
        end = next_day.astimezone(timezone.utc).isoformat()
    return start, end


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
        if self._db is not None:
            # 已注入 db（测试内存 fake）——视为已连接，不覆盖为真实 Mongo。
            return True
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
        """健康检查。若连接断开则尝试重连并重建索引（与 session_repo 对称）。

        Returns:
            True 表示可用，False 表示不可用。
        """
        if self._db is not None:
            return True
        # 启动期故障不应让归档永久失效——复用 connect() 的重连 + 建索引逻辑
        return await self.connect()

    # ---- core operations ----

    async def append(self, session_id: SessionId, msg: Message) -> None:
        """追加一条消息到永久归档。

        Args:
            session_id: 会话 ID。
            msg: 要归档的消息对象。
        """
        if self._db is None:
            # 永久归档静默丢失——必须留下可见日志（S1）。消息本体仍在 session
            # deque/JSONL，但 append-only message_archive 不落盘 = 永久丢失。
            logger.warning(
                "消息归档跳过：MessageRepository 未连接（MongoDB 不可用），"
                "本条消息不落 message_archive"
            )
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

    async def query_page(self, session_id: SessionId, *,
                         before: ObjectId | str | None = None,
                         limit: int = 20) -> tuple[list[Message], bool]:
        """按 session 键集分页查询（_id 游标）。

        批次 3（multi-session）：前端历史页用。limit+1 探测 has_more。

        Args:
            session_id: 会话 ID。
            before: _id 游标，只返回此 _id 之前的消息。
            limit: 每页条数，默认 20。

        Returns:
            (消息列表, has_more) 元组（按 _id 降序）。若数据库不可用返回 ([], False)。
        """
        if self._db is None:
            return [], False
        filter_expr: dict = {"session_id": session_id}
        if before is not None:
            oid = ObjectId(before) if isinstance(before, str) else before
            filter_expr["_id"] = {"$lt": oid}
        docs = await self._db.find(COLLECTION, filter_expr, sort=[("_id", -1)], limit=limit + 1)
        has_more = len(docs) > limit
        if has_more:
            docs = docs[:limit]
        messages = [_doc_to_message(doc) for doc in docs]
        return messages, has_more

    async def get_by_mongo_id(self, session_id: SessionId, doc_id: str) -> Message | None:
        """按 Mongo _id 取单条归档消息（资源索引分页续扫用——cursor 消息需从
        消息内 offset 继续扫）。不存在/非法 _id/库不可用返回 None。

        Args:
            session_id: 会话 ID。
            doc_id: Mongo ObjectId 字符串。

        Returns:
            对应 Message；找不到返回 None。
        """
        if self._db is None:
            return None
        try:
            oid = ObjectId(doc_id)
        except Exception:
            return None
        docs = await self._db.find(
            COLLECTION, {"session_id": session_id, "_id": oid}, limit=1)
        if not docs:
            return None
        return _doc_to_message(docs[0])

    async def get_by_id(self, msg_id: str) -> Message | None:
        """按 Mongo _id 取单条归档消息（跨会话，archive 删除联动取附件 part 用）。

        Args:
            msg_id: Mongo ObjectId 字符串（archive 删除端点的 msg_id）。

        Returns:
            对应 Message；不存在/非法 _id/库不可用返回 None。
        """
        if self._db is None:
            return None
        try:
            oid = ObjectId(msg_id)
        except Exception:
            return None
        docs = await self._db.find(COLLECTION, {"_id": oid}, limit=1)
        if not docs:
            return None
        return _doc_to_message(docs[0])

    async def query_after(self, session_id: SessionId, message_id: MessageId, *,
                          limit: int = 20) -> tuple[list[Message], bool]:
        """定位锚点消息（{session_id, message_id}）之后的消息，正序返回。

        Args:
            session_id: 会话 ID。
            message_id: 锚点消息 ID。
            limit: 每页条数，默认 20。

        Returns:
            (锚点之后的消息列表, has_more) 元组（按 _id 升序）。锚点不存在或
            数据库不可用时返回 ([], False)。
        """
        if self._db is None:
            return [], False
        anchor = await self._db.find(
            COLLECTION, {"session_id": session_id, "message_id": message_id}, limit=1)
        if not anchor:
            return [], False
        anchor_id = _oid(anchor[0]["_id"])
        filter_expr = {"session_id": session_id, "_id": {"$gt": anchor_id}}
        docs = await self._db.find(COLLECTION, filter_expr, sort=[("_id", 1)], limit=limit + 1)
        has_more = len(docs) > limit
        if has_more:
            docs = docs[:limit]
        messages = [_doc_to_message(doc) for doc in docs]
        return messages, has_more

    async def find_context(self, session_id: SessionId, message_id: MessageId, *,
                           before: int = 5, after: int = 5
                           ) -> tuple[Message | None, list[Message], list[Message]]:
        """定位锚点消息及其前后窗口（用于搜索结果的上下文展示）。

        Args:
            session_id: 会话 ID。
            message_id: 锚点消息 ID。
            before: 锚点之前取多少条，默认 5。
            after: 锚点之后取多少条，默认 5。

        Returns:
            (目标消息 | None, 之前消息列表(正序), 之后消息列表(正序))。
            锚点不存在返回 (None, [], [])；数据库不可用返回 (None, [], [])。
        """
        if self._db is None:
            return None, [], []
        anchor = await self._db.find(
            COLLECTION, {"session_id": session_id, "message_id": message_id}, limit=1)
        if not anchor:
            return None, [], []
        anchor_id = _oid(anchor[0]["_id"])
        target = _doc_to_message(anchor[0])
        pre_docs = await self._db.find(
            COLLECTION, {"session_id": session_id, "_id": {"$lt": anchor_id}},
            sort=[("_id", -1)], limit=before)
        # 之前窗口取 _id 降序最近 before 条，反转为正序
        pre_msgs = [_doc_to_message(doc) for doc in reversed(pre_docs)]
        post_docs = await self._db.find(
            COLLECTION, {"session_id": session_id, "_id": {"$gt": anchor_id}},
            sort=[("_id", 1)], limit=after)
        post_msgs = [_doc_to_message(doc) for doc in post_docs]
        return target, pre_msgs, post_msgs

    async def query_search(self, *, session_id: SessionId | None = None,
                           query: str = "", date_from: str | None = None,
                           date_to: str | None = None, timezone: str = "UTC",
                           category: str | None = None, cursor: str | None = None,
                           limit: int = 20
                           ) -> tuple[list[Message], bool, str | None]:
        """组合过滤 + keyset 分页搜索。

        组合 filter：session_id（None = 全部会话）、query（content.$elemMatch 正则）、
        自然日时间区间（date_from/date_to + IANA timezone，半开 UTC 区间）、
        category（part 类型 / role）。keyset 游标 base64(`timestamp|_id`)，
        排序 timestamp DESC, _id DESC；谓词 `(timestamp<T) OR (timestamp=T AND _id<ID)`。

        Args:
            session_id: 会话 ID，None 时跨全部会话。
            query: 内容搜索关键词（大小写不敏感）。
            date_from: 起始自然日 YYYY-MM-DD（含当天）。
            date_to: 结束自然日 YYYY-MM-DD（含当天）。
            timezone: IANA 时区名，默认 "UTC"。非法时区不抛异常，返回空结果。
            category: text|image|link|video|audio|file|tool。
            cursor: keyset 游标。
            limit: 每页条数，默认 20。

        Returns:
            (消息列表, has_more, next_cursor) 元组。数据库不可用或非法时区返回
            ([], False, None)。
        """
        if self._db is None:
            return [], False, None
        filter_expr: dict = {}
        if session_id is not None:
            filter_expr["session_id"] = session_id

        # 内容匹配：query 与 category 拆两条 $elemMatch 用 $and（不同 part 元素可分别命中）
        elem_text: dict = {}   # 关键词只命中 text part 的 content
        elem_type: dict = {}   # category 命中对应 part 的 type
        if query:
            elem_text = {"type": "text",
                         "content": {"$regex": query, "$options": "i"}}
        if category == "link":
            elem_type = {"type": "text",
                         "content": {"$regex": r"(https?://|www\.)", "$options": "i"}}
        elif category in _CATEGORY_PART_TYPES - {"text", "link"}:
            elem_type = {"type": category}
        elif category == "text" and not query:
            elem_type = {"type": "text"}
        elem = []
        if elem_text:
            elem.append({"$elemMatch": elem_text})
        if elem_type:
            elem.append({"$elemMatch": elem_type})
        if len(elem) == 1:
            filter_expr["content"] = elem[0]
        elif len(elem) == 2:
            filter_expr["$and"] = [{"content": e} for e in elem]
        if category == "tool":
            filter_expr["role"] = "tool"

        # 自然日 → 半开 UTC 区间 [dayStart(date_from), nextDayStart(date_to))
        if date_from or date_to:
            try:
                tz = ZoneInfo(timezone)
                start, end = _build_date_bounds(date_from, date_to, tz)
                ts_range: dict = {}
                if start is not None:
                    ts_range["$gte"] = start
                if end is not None:
                    ts_range["$lt"] = end
                filter_expr["timestamp"] = ts_range
            except Exception:
                # 非法时区/日期格式：仓库方法不抛异常，返回空结果（调用方抛 INVALID_TIMEZONE）
                return [], False, None

        # keyset 游标
        if cursor:
            try:
                ts, raw_id = _decode_cursor(cursor)
            except Exception:
                return [], False, None
            filter_expr["$or"] = [
                {"timestamp": {"$lt": ts}},
                {"timestamp": ts, "_id": {"$lt": _oid(raw_id)}},
            ]

        docs = await self._db.find(
            COLLECTION, filter_expr,
            sort=[("timestamp", -1), ("_id", -1)], limit=limit + 1)
        has_more = len(docs) > limit
        if has_more:
            docs = docs[:limit]
        messages = [_doc_to_message(doc) for doc in docs]
        next_cursor: str | None = None
        if has_more and docs:
            next_cursor = _encode_cursor(docs[-1].get("timestamp", ""), docs[-1]["_id"])
        return messages, has_more, next_cursor

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
