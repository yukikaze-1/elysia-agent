"""SessionRepository — MongoDB sessions 集合的 Repository 实现。"""

import logging
from dataclasses import dataclass
from typing import Sequence

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema._utils import now_iso
from core.schema.adapters.message_adapter import validate_message_legacy_compat
from core.schema.character import CharacterState
from core.schema.memory_reflector import MemoryReflectorState
from core.schema.message import Message
from core.schema.types import IsoTimestamp, SessionId

from .base import Repository

logger = logging.getLogger(__name__)
COLLECTION = "sessions"


@dataclass
class SessionSummary:
    """会话摘要信息，用于列表展示。"""
    session_id: SessionId
    message_count: int
    updated_at: IsoTimestamp


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
                   reflector_state: MemoryReflectorState | None = None) -> None:
        """保存或更新 session。

        Args:
            session_id: 会话 ID。
            messages: 消息列表。
            character_state: 角色状态。
            reflector_state: 记忆反射器状态，为 None 时写入空对象。
        """
        if self._db is None:
            return
        serialized = [m.model_dump(mode="json") for m in messages]
        doc = {
            "session_id": session_id,
            "messages": serialized,
            "character_state": character_state.model_dump(mode="json"),
            "reflector_state": reflector_state.model_dump(mode="json") if reflector_state else {},
            "updated_at": now_iso(),
        }
        await self._db.update_one(
            COLLECTION, {"session_id": session_id}, {"$set": doc}, upsert=True,
        )

    async def load(self, session_id: SessionId
                   ) -> tuple[list[Message], CharacterState, MemoryReflectorState] | None:
        """加载 session。

        Args:
            session_id: 会话 ID。

        Returns:
            (消息列表, 角色状态, 记忆反射器状态) 元组，若不存在或数据库不可用返回 None。
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
        return messages, cs, rs

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
