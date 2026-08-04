"""SessionRepository — MongoDB sessions 集合的 Repository 实现。"""

import logging
from dataclasses import dataclass
from typing import Sequence

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema.utils import now_iso
from core.schema.adapters.message_adapter import validate_message_legacy_compat
from core.schema.character import CharacterState
from core.schema.memory_reflector import MemoryReflectorState
from core.schema.message import Message, MessageRole
from core.schema.part import TextPart
from core.schema.service_state import ServiceState
from core.schema.types import IsoTimestamp, SessionId

from core.storage.base import Repository

logger = logging.getLogger(__name__)
COLLECTION = "sessions"

AUTO_TITLE_MAX_LEN = 20
"""自动标题最大长度（首条 UserMessage 文本截取字符数）。"""


def _derive_title(messages: Sequence[Message]) -> str:
    """从首条 UserMessage 的文本内容派生自动标题（前 20 字符）。

    幂等派生：仅在 session 文档无显式 title 时由 save() 调用，
    首条 UserMessage 无文本（如纯图片）时返回空串。

    Args:
        messages: 消息序列。

    Returns:
        派生标题，可能为空串。
    """
    for m in messages:
        if getattr(m, "role", None) != MessageRole.USER:
            continue
        text = "".join(p.content for p in getattr(m, "content", ()) if isinstance(p, TextPart))
        text = text.strip()
        return text[:AUTO_TITLE_MAX_LEN]
    return ""


@dataclass
class SessionSummary:
    """会话摘要信息，用于列表展示。"""
    session_id: SessionId
    message_count: int
    updated_at: IsoTimestamp
    title: str = ""
    active: bool = False  # 是否为当前活动会话（由 SessionManager.list 填充）


class SessionRepository(Repository):
    """会话存储 — MongoDB sessions 集合。"""

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 database_name: str = "default",
                 base_url: str | None = None):
        """初始化会话存储。

        Args:
            db: 可选的文档数据库后端，为 None 时需调用 connect() 建立连接。
            database_name: MongoDB 数据库名称，默认 "default"。
            base_url: MongoDB 连接串，为 None 时从 MONGODB_URI 环境变量读取。
        """
        self._db: DocumentDBBackend | None = db
        self._database_name = database_name
        self._base_url = base_url

    async def connect(self) -> bool:
        """建立 MongoDB 连接，创建索引和 TTL。

        Returns:
            True 表示连接成功，False 表示连接失败。
        """
        try:
            self._db = DocumentDBFactory.create(
                f"mongodb/{self._database_name}",
                base_url=self._base_url,
            )
            await self._db.ensure_index(COLLECTION, [("session_id", 1)])
            await self._db.ensure_index(COLLECTION, [("updated_at", -1)])
            try:
                await self._db.ensure_index(
                    COLLECTION, [("updated_at", 1)], expireAfterSeconds=30 * 86400)
            except Exception as e:
                logger.warning("SessionRepository: TTL index failed (%s)", e)
            logger.info("SessionRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("SessionRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        """关闭数据库连接，释放资源。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        """健康检查。若连接断开则尝试重连并重建索引。

        Returns:
            True 表示可用，False 表示不可用。
        """
        if self._db is not None:
            return True
        try:
            self._db = DocumentDBFactory.create(
                f"mongodb/{self._database_name}",
                base_url=self._base_url,
            )
            await self._db.ensure_index(COLLECTION, [("session_id", 1)])
            await self._db.ensure_index(COLLECTION, [("updated_at", -1)])
            try:
                await self._db.ensure_index(
                    COLLECTION, [("updated_at", 1)], expireAfterSeconds=30 * 86400)
            except Exception as e:
                logger.warning("TTL index creation failed (%s)", e)
            logger.info("SessionRepository: MongoDB 已恢复连接")
            return True
        except Exception:
            return False

    async def save(self, session_id: SessionId, messages: Sequence[Message],
                   character_state: CharacterState,
                   reflector_state: MemoryReflectorState | None = None,
                   service_state: ServiceState | None = None) -> None:
        """保存或更新 session。

        Args:
            session_id: 会话 ID。
            messages: 消息列表。
            character_state: 角色状态。
            reflector_state: 记忆反射器状态，为 None 时写入空对象。
            service_state: 服务运行时状态，为 None 时写入空对象。
        """
        if self._db is None:
            return
        serialized = [m.model_dump(mode="json") for m in messages]
        doc = {
            "session_id": session_id,
            "messages": serialized,
            "character_state": character_state.model_dump(mode="json"),
            "reflector_state": reflector_state.model_dump(mode="json") if reflector_state else {},
            "service_state": service_state.model_dump(mode="json") if service_state else {},
            "updated_at": now_iso(),
        }
        # 标题语义：显式 title（create/rename 写入）必须保留；
        # 仅当现有 doc 无 title 且 messages 非空时派生自动标题（幂等，无额外调用点）。
        existing = await self._db.find(COLLECTION, {"session_id": session_id}, limit=1)
        existing_title = existing[0].get("title", "") if existing else ""
        doc["title"] = existing_title if existing_title else _derive_title(messages)
        await self._db.update_one(
            COLLECTION, {"session_id": session_id}, {"$set": doc}, upsert=True,
        )

    async def create(self, session_id: SessionId, title: str = "") -> None:
        """创建会话（写入 title 与空消息体）。

        create 后 title 由 save() 保留（save 不覆盖非空 title 除非显式）。

        Args:
            session_id: 会话 ID。
            title: 会话标题，默认空串（可由后续 save 自动派生）。
        """
        if self._db is None:
            return
        doc = {
            "session_id": session_id,
            "title": title,
            "messages": [],
            "updated_at": now_iso(),
        }
        await self._db.update_one(
            COLLECTION, {"session_id": session_id}, {"$set": doc}, upsert=True,
        )

    async def rename(self, session_id: SessionId, title: str) -> bool:
        """重命名会话。

        Args:
            session_id: 会话 ID。
            title: 新标题。

        Returns:
            True 表示会话存在且已更新，False 表示会话不存在或数据库不可用。
        """
        if self._db is None:
            return False
        result = await self._db.update_one(
            COLLECTION, {"session_id": session_id}, {"$set": {"title": title}},
            upsert=False,
        )
        return result > 0

    async def load(self, session_id: SessionId
                   ) -> tuple[list[Message], CharacterState, MemoryReflectorState, ServiceState] | None:
        """加载 session。

        Args:
            session_id: 会话 ID。

        Returns:
            (消息列表, 角色状态, 记忆反射器状态, 服务运行时状态) 元组，若不存在或数据库不可用返回 None。
        """
        if self._db is None:
            return None
        results = await self._db.find(COLLECTION, {"session_id": session_id}, limit=1)
        if not results:
            return None
        doc = results[0]
        msgs_raw = doc.get("messages", [])
        for m in msgs_raw:
            m.pop("_id", None)
        messages = [validate_message_legacy_compat(m) for m in msgs_raw]
        cs_data = doc.get("character_state", {})
        cs = CharacterState.model_validate(cs_data) if cs_data else CharacterState(session_id=session_id)
        rs_data = doc.get("reflector_state", {})
        rs = MemoryReflectorState.model_validate(rs_data) if rs_data else MemoryReflectorState()
        ss_data = doc.get("service_state", {})
        ss = ServiceState.model_validate(ss_data) if ss_data else ServiceState()
        return messages, cs, rs, ss

    async def delete(self, session_id: SessionId) -> int:
        """删除指定会话。

        Args:
            session_id: 会话 ID。

        Returns:
            删除的会话数。若数据库不可用返回 0。
        """
        if self._db is None:
            return 0
        result = await self._db.delete_many(COLLECTION, {"session_id": session_id})
        logger.info("SessionRepository delete: session_id=%s, deleted=%d", session_id, result)
        return result

    async def list_all(self) -> list[SessionSummary]:
        """列出最近会话摘要。

        Returns:
            会话摘要列表，按更新时间降序，最多 200 条。若数据库不可用返回空列表。
        """
        if self._db is None:
            return []
        docs = await self._db.find(COLLECTION, {}, sort=[("updated_at", -1)], limit=200)
        return [
            SessionSummary(
                session_id=SessionId(d.get("session_id", "")),
                message_count=len(d.get("messages", [])),
                updated_at=d.get("updated_at", ""),
                title=d.get("title", ""),
            )
            for d in docs
        ]

    async def clear_all(self) -> int:
        """清空全部会话。

        Returns:
            删除的会话数。若数据库不可用返回 0。
        """
        if self._db is None:
            return 0
        result = await self._db.delete_many(COLLECTION, {})
        logger.warning("SessionRepository clear_all: deleted=%d sessions", result)
        return result
