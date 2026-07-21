"""CharacterVersionRepository — MongoDB character_l2_versions 集合。"""
import logging
from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema.types import SessionId
from core.schema.compression import CompressionTrigger, VersionEntry
from .base import Repository

if TYPE_CHECKING:
    from core.schema.types import IsoTimestamp

logger = logging.getLogger(__name__)
COLLECTION = "character_l2_versions"


class CharacterVersionRepository(Repository):
    """L2 Self 版本历史存储 — append-only, 永久保留。"""

    def __init__(self, db: DocumentDBBackend | None = None):
        self._db: DocumentDBBackend | None = db

    # ---- lifecycle ----

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
            await self._db.ensure_index(COLLECTION, [
                ("session_id", 1), ("version", -1),
            ])
            logger.info("CharacterVersionRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("CharacterVersionRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return self._db is not None

    # ---- core API ----

    async def append_version(
        self, session_id: SessionId, text: str,
        timestamp: "IsoTimestamp", trigger: CompressionTrigger,
        change_summary: list[str],
    ) -> int:
        """追加一个版本。自动计算 version 序号（max+1）。返回新版本号。"""
        if self._db is None:
            return 0
        # 计算下一个版本号
        docs = await self._db.find(
            COLLECTION,
            {"session_id": session_id},
            sort=[("version", -1)],
            limit=1,
        )
        next_version = (docs[0]["version"] + 1) if docs else 1
        doc = {
            "session_id": session_id,
            "version": next_version,
            "text": text,
            "timestamp": timestamp,
            "trigger": trigger.value,
            "change_summary": change_summary,
        }
        await self._db.insert_one(COLLECTION, doc)
        return next_version

    async def list_versions(
        self, session_id: SessionId, *,
        limit: int = 20, before_version: int | None = None,
    ) -> list[VersionEntry]:
        """分页查询版本历史。默认返回最近 20 条。"""
        if self._db is None:
            return []
        filter_expr: dict = {"session_id": session_id}
        if before_version is not None:
            filter_expr["version"] = {"$lt": before_version}
        docs = await self._db.find(
            COLLECTION, filter_expr,
            sort=[("version", -1)], limit=limit,
        )
        return [
            VersionEntry(
                session_id=doc["session_id"],
                version=doc["version"],
                text=doc["text"],
                timestamp=doc["timestamp"],
                trigger=doc["trigger"],
                change_summary=doc.get("change_summary", []),
            )
            for doc in docs
        ]

    async def get_version(
        self, session_id: SessionId, version: int,
    ) -> VersionEntry | None:
        """获取指定版本。"""
        if self._db is None:
            return None
        docs = await self._db.find(
            COLLECTION,
            {"session_id": session_id, "version": version},
            limit=1,
        )
        if not docs:
            return None
        doc = docs[0]
        return VersionEntry(
            session_id=doc["session_id"],
            version=doc["version"],
            text=doc["text"],
            timestamp=doc["timestamp"],
            trigger=doc["trigger"],
            change_summary=doc.get("change_summary", []),
        )

    async def count_versions(self, session_id: SessionId) -> int:
        """返回版本总数。"""
        if self._db is None:
            return 0
        return await self._db.count(COLLECTION, {"session_id": session_id})
