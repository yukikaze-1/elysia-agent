"""SessionRepository — MongoDB sessions 集合的 Repository 实现。"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
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

    def __init__(self, db: DocumentDBBackend | None = None):
        self._db: DocumentDBBackend | None = db

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
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
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        if self._db is not None:
            return True
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
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

    async def save(self, session_id: str, messages: Sequence[Any],
                   params: dict[str, Any]) -> None:
        """保存或更新 session。"""
        if self._db is None:
            return
        # 序列化 messages（支持 Pydantic Message 对象和原始 dict）
        serialized = [
            m.model_dump(mode="json") if hasattr(m, "model_dump") else m
            for m in messages
        ]
        doc = {
            "session_id": session_id,
            "messages": serialized,
            "params": params,
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        await self._db.update_one(
            COLLECTION, {"session_id": session_id}, {"$set": doc}, upsert=True,
        )

    async def load(self, session_id: str
                   ) -> tuple[list[dict], dict[str, Any]] | None:
        """加载 session。返回 (messages, params) 或 None。"""
        if self._db is None:
            return None
        results = await self._db.find(COLLECTION, {"session_id": session_id}, limit=1)
        if not results:
            return None
        doc = results[0]
        for m in doc.get("messages", []):
            m.pop("_id", None)
        return doc.get("messages", []), dict(doc.get("params", {}))

    async def delete(self, session_id: str) -> int:
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {"session_id": session_id})

    async def list_all(self) -> list[SessionSummary]:
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
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {})
