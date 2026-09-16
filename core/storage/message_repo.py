"""MessageRepository — MongoDB message_archive 集合操作。"""

import base64
import logging
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bson import ObjectId
from bson.errors import InvalidId

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from core.storage.base import (
    Repository,
    StorageUnavailableError,
    search_regex_filter,
)
from core.storage.character_write_guard import (
    is_assistant_without_character,
    warn_missing_character_snapshot,
)
from core.storage.destructive_guard import require_confirm
from core.schema.adapters.message_adapter import validate_message_legacy_compat
from core.schema.types import MessageId, SessionId
from core.schema.message import Message, MessageRole
from frontend_contract.rest import SearchCategory

logger = logging.getLogger(__name__)

COLLECTION = "message_archive"

CONFIRM_TOKEN = "archive"
"""破坏性 `clear_all` 的确认令牌（稳定资源名——理由见 core/storage/destructive_guard.py）。"""

ASSISTANT_WITHOUT_CHARACTER_FILTER: dict = {
    "role": "assistant",
    "$or": [{"character": None}, {"character": {"$exists": False}}],
}
"""缺少角色身份快照的 assistant 归档消息过滤条件（多角色完整性自检用）。

`character_profiles=true` 时前端要求每条 assistant 消息都带角色身份快照，缺失会让
消息历史整批拒绝，故启动期需能统计这类记录。
"""

# query_search 支持的 category 取值 → content part 类型 / role 映射（D3-B 枚举派生单源）。
# tool 特殊：无 part type，走 role=tool；link 特殊：archive 无独立 link part，URL 正则特判。
_CATEGORY_PART_TYPES = frozenset(m.value for m in SearchCategory) - {"tool"}


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


def _build_search_filter(*, session_id: SessionId | None, query: str,
                          date_from: str | None, date_to: str | None,
                          timezone: str, category: str | None) -> dict | None:
    """组装 query_search 的 Mongo filter；非法参数返回 None（调用方返回空结果）。"""
    filter_expr: dict = {}
    if session_id is not None:
        filter_expr["session_id"] = session_id

    elem_text: dict = {}
    elem_type: dict = {}
    if query:
        clause = search_regex_filter(query)      # S2-B7c：转义 + 上限单源
        if clause is None:
            return None
        elem_text = {"type": "text", "content": clause}
    if category == SearchCategory.LINK:
        elem_type = {"type": "text",
                     "content": {"$regex": r"(https?://|www\.)", "$options": "i"}}
    elif category in _CATEGORY_PART_TYPES - {"text", "link"}:
        elem_type = {"type": category}
    elif category == SearchCategory.TEXT and not query:
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
    if category == SearchCategory.TOOL:
        filter_expr["role"] = "tool"

    if date_from or date_to:
        try:
            tz = ZoneInfo(timezone)
            start, end = _build_date_bounds(date_from, date_to, tz)
        except Exception:
            return None
        ts_range: dict = {}
        if start is not None:
            ts_range["$gte"] = start
        if end is not None:
            ts_range["$lt"] = end
        filter_expr["timestamp"] = ts_range
    return filter_expr


def _to_object_id(msg_id: MessageId) -> ObjectId:
    """消息 ID → ObjectId；非法值抛 `InvalidMessageIdError`（S3-19）。

    此前 `ObjectId(msg_id)` 裸调用 → `bson.InvalidId` 冒泡成 500。畸形 ID 是**客户端
    错误**，应由路由映射成 400 `CONTRACT_VALIDATION`（与 before/after 游标同一约定）。

    Args:
        msg_id: wire 上的消息 ID（24-hex）。

    Returns:
        对应的 ObjectId。

    Raises:
        InvalidMessageIdError: 不是合法 ObjectId。
    """
    try:
        return ObjectId(msg_id)
    except (InvalidId, TypeError) as e:
        raise InvalidMessageIdError(f"非法消息 ID: {msg_id!r}") from e


class InvalidMessageIdError(ValueError):
    """消息 ID 不是合法的 Mongo ObjectId（S3-19）。

    继承 `ValueError`：调用方既可精确捕获，也不破坏既有 `except ValueError` 语义。
    此前 `ObjectId(msg_id)` 裸调用，畸形 ID 抛 `bson.InvalidId` 一路冒泡成 **500**——
    那是**客户端错误**，应当是 400。
    """


class MessageRepository(Repository):
    """消息归档存储。"""

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 database_name: str = "default",
                 base_url: str | None = None,
                 require_assistant_character: bool = False):
        """初始化消息归档存储。

        Args:
            db: 可选的文档数据库后端，为 None 时需调用 connect() 建立连接。
            database_name: MongoDB 数据库名称，默认 "default"。
            base_url: MongoDB 连接串，为 None 时从 MONGODB_URI 环境变量读取。
            require_assistant_character: `character_profiles` 能力开启时置 True——
                append() 落库 assistant 消息若缺角色身份快照，发显著告警（仍写入，
                见 core/storage/character_write_guard.py 的设计取舍）。
        """
        self._db: DocumentDBBackend | None = db
        self._database_name = database_name
        self._base_url = base_url
        self._injected_db = db is not None
        self._require_assistant_character = require_assistant_character

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
        """健康检查：真实 ping（P1-05），失败时经 connect() 重连 + 建索引。

        Returns:
            True 表示可用，False 表示不可用。
        """
        return await self._health_check()

    # ---- core operations ----

    def _require_db(self, op: str) -> DocumentDBBackend:
        """历史**读**路径的 fail-closed 守卫（S2-E5）。

        Mongo 离线时这些方法原先返回空页/0，REST 层再回 `200 {"messages":[]}`——
        而同一系统的 `/ready` 探针是 **fail-closed**（503）。**同一系统对"存储不可用"
        给出两种相反信号**：用户看到"历史空了"且无错误、不重试，甚至可能据此误删会话。

        覆盖本仓储**全部**读路径（决策 #8，owner 2026-09-16「整仓统一 fail-closed」）：
        离线时"读不到"不得长得像"没有"。此前按调用方逐个决定（批次 6/7 #25），留下的是
        "同一仓储里同类读一半 503 一半返回空"——调用方无从判断自己拿到的是哪种。

        需要降级的地方在**调用方**显式做，并留下可观测痕迹（例：
        `DebugInfo._collect_session_and_memory` 捕获 `count()` 后留空面板），
        而不是让仓储安静返回空。

        Raises:
            StorageUnavailableError: 数据库未连接。
        """
        if self._db is None:
            raise StorageUnavailableError(
                f"MessageRepository 未连接——{op} 无法判定，"
                f"不得把「存储不可用」报成「没有消息」")
        return self._db

    async def append(self, session_id: SessionId, msg: Message) -> None:
        """追加一条消息到永久归档。

        Args:
            session_id: 会话 ID。
            msg: 要归档的消息对象。
        """
        # 归档 ID 非空不变量（spec D4）：数据不变量与存储可用性无关，置于 db-None
        # 分支之前——否则 Mongo 离线时空 ID 被「DB 不可用」warning 掩盖违规。
        # append 是 fire-and-forget 归档，此 ValueError 会被 session._archive_done
        # 降级为 warning；同步 fail-fast 靠模型校验器（Task 2）。
        if not msg.message_id:
            raise ValueError(
                f"消息归档拒绝空 message_id（role={msg.role}，session={session_id}）"
                "——生产写入路径必须分配非空 ID")
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
        # P3-3 写入期兜底：assistant 缺角色快照 → 显著告警（仍写入；见 guard 模块）。
        if self._require_assistant_character and is_assistant_without_character(msg):
            warn_missing_character_snapshot(
                collection=COLLECTION, session_id=str(session_id),
                missing=1, total=1)
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
            (消息列表, 总条数) 元组。
        Raises:
            StorageUnavailableError: 数据库未连接（决策 #8：本仓储整仓读 fail-closed，
                离线时不得把「读不到」报成「没有」）。
        """
        db = self._require_db("query_by_session")
        filter_expr: dict = {"session_id": session_id}
        if before:
            filter_expr["_id"] = {"$lt": before}
        sort = [("_id", -1)]
        docs = await db.find(COLLECTION, filter_expr, sort=sort, skip=skip, limit=limit)
        total = await db.count(COLLECTION, {"session_id": session_id})
        messages = [_doc_to_message(doc) for doc in docs]
        return messages, total

    async def query(self, *, before: ObjectId | str | None = None,
                    limit: int = 20) -> tuple[list[Message], bool]:
        """基于游标的分页查询（/api/messages 使用）。

        Args:
            before: 游标，只返回此 ObjectId 之前的消息。
            limit: 每页条数，默认 20。

        Returns:
            (消息列表, has_more) 元组。
        Raises:
            StorageUnavailableError: 数据库未连接（决策 #8：本仓储整仓读 fail-closed，
                离线时不得把「读不到」报成「没有」）。
        """
        db = self._require_db("query")
        filter_expr: dict = {}
        if before:
            oid = ObjectId(before) if isinstance(before, str) else before
            filter_expr["_id"] = {"$lt": oid}
        sort = [("_id", -1)]
        docs = await db.find(COLLECTION, filter_expr, sort=sort, limit=limit + 1)
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
            (消息列表, has_more) 元组（按 _id 降序）。

        Raises:
            StorageUnavailableError: 数据库未连接（S2-E5——不得报成"没有消息"）。
        """
        db = self._require_db("query_page")
        filter_expr: dict = {"session_id": session_id}
        if before is not None:
            oid = ObjectId(before) if isinstance(before, str) else before
            filter_expr["_id"] = {"$lt": oid}
        docs = await db.find(COLLECTION, filter_expr, sort=[("_id", -1)], limit=limit + 1)
        has_more = len(docs) > limit
        if has_more:
            docs = docs[:limit]
        messages = [_doc_to_message(doc) for doc in docs]
        return messages, has_more

    async def get_by_mongo_id(self, session_id: SessionId, doc_id: str) -> Message | None:
        """按 Mongo _id 取单条归档消息（资源索引分页续扫用——cursor 消息需从
        消息内 offset 继续扫）。不存在/非法 _id 返回 None。

        Args:
            session_id: 会话 ID。
            doc_id: Mongo ObjectId 字符串。

        Returns:
            对应 Message；找不到/非法 _id 返回 None。
        Raises:
            StorageUnavailableError: 数据库未连接（决策 #8：本仓储整仓读 fail-closed，
                离线时不得把「读不到」报成「没有」）。
        """
        db = self._require_db("get_by_mongo_id")
        try:
            oid = ObjectId(doc_id)
        except Exception:
            return None
        docs = await db.find(
            COLLECTION, {"session_id": session_id, "_id": oid}, limit=1)
        if not docs:
            return None
        return _doc_to_message(docs[0])

    async def get_by_id(self, msg_id: str) -> Message | None:
        """按 Mongo _id 取单条归档消息（跨会话，archive 删除联动取附件 part 用）。

        Args:
            msg_id: Mongo ObjectId 字符串（archive 删除端点的 msg_id）。

        Returns:
            对应 Message；不存在/非法 _id 返回 None。
        Raises:
            StorageUnavailableError: 数据库未连接（决策 #8：本仓储整仓读 fail-closed，
                离线时不得把「读不到」报成「没有」）。
        """
        db = self._require_db("get_by_id")
        try:
            oid = ObjectId(msg_id)
        except Exception:
            return None
        docs = await db.find(COLLECTION, {"_id": oid}, limit=1)
        if not docs:
            return None
        return _doc_to_message(docs[0])

    async def query_after(self, session_id: SessionId, after_oid: ObjectId, *,
                          limit: int = 20) -> tuple[list[Message], bool]:
        """定位锚点 ObjectId 之后的消息，正序返回（_id > after_oid 过滤器）。

        9A-2：锚点从 message_id 改为 Mongo _id——不再按 message_id 查找锚点消息
        （无缺失特判），直接过滤 _id > after_oid；锚点为会话最新（或所有更近消息
        已删）时返回空页 + False。

        Args:
            session_id: 会话 ID。
            after_oid: 锚点消息的 Mongo ObjectId（游标）。
            limit: 每页条数，默认 20。

        Returns:
            (锚点之后的消息列表, has_more) 元组（按 _id 升序）。

        Raises:
            StorageUnavailableError: 数据库未连接（S2-E5——不得报成"没有消息"）。
        """
        db = self._require_db("query_after")
        filter_expr = {"session_id": session_id, "_id": {"$gt": after_oid}}
        docs = await db.find(COLLECTION, filter_expr, sort=[("_id", 1)], limit=limit + 1)
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
            锚点不存在返回 (None, [], [])。
        Raises:
            StorageUnavailableError: 数据库未连接（决策 #8：本仓储整仓读 fail-closed，
                离线时不得把「读不到」报成「没有」）。
        """
        db = self._require_db("find_context")
        anchor = await db.find(
            COLLECTION, {"session_id": session_id, "message_id": message_id}, limit=1)
        if not anchor:
            return None, [], []
        anchor_id = _oid(anchor[0]["_id"])
        target = _doc_to_message(anchor[0])
        pre_docs = await db.find(
            COLLECTION, {"session_id": session_id, "_id": {"$lt": anchor_id}},
            sort=[("_id", -1)], limit=before)
        # 之前窗口取 _id 降序最近 before 条，反转为正序
        pre_msgs = [_doc_to_message(doc) for doc in reversed(pre_docs)]
        post_docs = await db.find(
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
            category: text|image|link|video|audio|file|tool|sticker。
            cursor: keyset 游标。
            limit: 每页条数，默认 20。

        Returns:
            (消息列表, has_more, next_cursor) 元组。非法时区（或游标损坏）返回
            ([], False, None)。
        Raises:
            StorageUnavailableError: 数据库未连接（决策 #8：本仓储整仓读 fail-closed，
                离线时不得把「读不到」报成「没有」）。
        """
        db = self._require_db("query_search")
        filter_expr = _build_search_filter(
            session_id=session_id, query=query, date_from=date_from,
            date_to=date_to, timezone=timezone, category=category)
        if filter_expr is None:
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

        docs = await db.find(
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
            消息总数。

        Raises:
            StorageUnavailableError: 数据库未连接（S2-E5——不得报成"0 条"）。
        """
        db = self._require_db("count_by_session")
        return await db.count(COLLECTION, {"session_id": session_id})

    async def count(self) -> int:
        """返回全部消息总数。

        Returns:
            消息总数。
        Raises:
            StorageUnavailableError: 数据库未连接（决策 #8：本仓储整仓读 fail-closed，
                离线时不得把「读不到」报成「没有」）。
        """
        db = self._require_db("count")
        return await db.count(COLLECTION, {})

    async def count_assistant_without_character(self) -> int:
        """统计缺少角色身份快照的 assistant 归档消息数（多角色完整性自检）。

        数据库侧 count，不拉取文档。用于启动期自检告警：`character_profiles=true`
        时这类记录会让前端对消息历史整批拒绝。

        Returns:
            命中的 assistant 消息数。

        Raises:
            StorageUnavailableError: MongoDB 未连接（区分「0 条缺口」与「未扫描」——
                调用方据此标记自检未执行，而非误报数据健康）。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "MessageRepository 未连接（MongoDB 不可用），无法执行消息角色完整性自检")
        return await self._db.count(COLLECTION, ASSISTANT_WITHOUT_CHARACTER_FILTER)

    async def delete_by_session(self, session_id: SessionId) -> int:
        """按会话删除所有消息。

        Args:
            session_id: 会话 ID。

        Returns:
            删除的消息数（0 = 无记录，幂等）。

        Raises:
            StorageUnavailableError: MongoDB 未连接（B3/P1-03——离线删除不得
                静默返回 0 被 caller 当成功，否则删除后归档记录复活）。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "MessageRepository 未连接（MongoDB 不可用），消息未删除")
        result = await self._db.delete_many(COLLECTION, {"session_id": session_id})
        logger.info("MessageRepository delete_by_session: session_id=%s, deleted=%d", session_id, result)
        return result

    async def clear_all(self, *, confirm: str) -> int:
        """清空全部消息归档（**破坏性且不可逆**，要求类型化确认）。

        Args:
            confirm: 必须等于 `CONFIRM_TOKEN`（破坏性操作的目标资源确认）。

        Returns:
            删除的消息数。

        Raises:
            DestructiveConfirmRequired: confirm 不匹配。
            StorageUnavailableError: 数据库未连接（此前静默返回 0，调用方会
                误以为已清空，现改 fail-closed）。
        """
        require_confirm(
            target=CONFIRM_TOKEN, confirm=confirm, operation="清空消息归档",
        )
        if self._db is None:
            raise StorageUnavailableError(
                f"MessageRepository.clear_all: {COLLECTION} 未连接，拒绝静默成功",
            )
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
            (消息列表, 总条数) 元组。
        Raises:
            StorageUnavailableError: 数据库未连接（决策 #8：本仓储整仓读 fail-closed，
                离线时不得把「读不到」报成「没有」）。
        """
        db = self._require_db("query_offset")
        filter_expr: dict = {}
        if search:
            # S2-B7c：用户输入不得当正则执行（转义 + 长度上限走收口助手）。
            clause = search_regex_filter(search)
            if clause is None:
                logger.warning("归档搜索词非法（超长 %d）——按无匹配处理", len(search))
                return [], 0
            filter_expr["content"] = {"$elemMatch": {"content": clause}}
        if role is not None:
            filter_expr["role"] = role.value
        sort = [("_id", -1)]
        docs = await db.find(COLLECTION, filter_expr, sort=sort, skip=skip, limit=limit)
        total = await db.count(COLLECTION, filter_expr)
        messages = [_doc_to_message(doc) for doc in docs]
        return messages, total

    async def delete_one(self, msg_id: MessageId) -> int:
        """按 ID 删除单条消息。

        Args:
            msg_id: 消息 ID。

        Returns:
            删除的消息数。

        Raises:
            StorageUnavailableError: 数据库未连接（S3-19——原先静默返回 0，
                调用方无法区分"没删到"与"存储不可用"；同类的
                `delete_by_session` / `clear_all` 早已 fail-closed，只有这两个写方法漏改）。
            InvalidMessageIdError: `msg_id` 不是合法 ObjectId（应映射 400 而非 500）。
        """
        db = self._require_db("delete_one")
        result = await db.delete_many(COLLECTION, {"_id": _to_object_id(msg_id)})
        logger.info("MessageRepository delete_one: msg_id=%s, deleted=%d", msg_id, result)
        return result

    async def update_text(self, msg_id: MessageId, new_text: str) -> int:
        """更新消息文本内容。

        Args:
            msg_id: 消息 ID。
            new_text: 新的文本内容。

        Returns:
            修改的记录数。

        Raises:
            StorageUnavailableError: 数据库未连接（S3-19，同上）。
            InvalidMessageIdError: `msg_id` 不是合法 ObjectId。
        """
        db = self._require_db("update_text")
        result = await db.update_one(
            COLLECTION,
            {"_id": _to_object_id(msg_id)},
            {"$set": {"content": [{"type": "text", "content": new_text}]}},
        )
        logger.info("MessageRepository update_text: msg_id=%s, text_len=%d, modified=%d", msg_id, len(new_text), result)
        return result
