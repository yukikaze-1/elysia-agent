"""CharacterVersionRepository — MongoDB character_versions 集合。"""
import logging
from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema.types import SessionId
from core.schema.compression import CompressionTrigger, VersionEntry
from .base import Repository

if TYPE_CHECKING:
    from core.schema.types import IsoTimestamp

logger = logging.getLogger(__name__)
COLLECTION = "character_versions"       # 改名（原 character_l2_versions）


class CharacterVersionRepository(Repository):
    """角色 Section 版本历史存储 — append-only, 永久保留。
    支持多种 content_type: "l2", "c6", 未来扩展 "c7", "c2" 等。"""

    def __init__(self, db: DocumentDBBackend | None = None):
        self._db: DocumentDBBackend | None = db

    # ---- lifecycle ----

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
            # 检查脏数据（重复文档），有重复时降级为非唯一索引
            dup_pipeline = [
                {"$group": {
                    "_id": {"sid": "$session_id", "ct": "$content_type", "v": "$version"},
                    "count": {"$sum": 1},
                }},
                {"$match": {"count": {"$gt": 1}}},
            ]
            try:
                dups = await self._db.aggregate(COLLECTION, dup_pipeline)
            except Exception:
                dups = []  # aggregate 不支持时跳过检查
            unique = len(list(dups)) == 0 if dups else True
            if not unique:
                logger.warning(
                    "CharacterVersionRepository: 发现重复文档，降级为非唯一索引")
            await self._db.ensure_index(COLLECTION, [
                ("session_id", 1), ("content_type", 1), ("version", -1),
            ], unique=unique)
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
        content_type: str = "l2",
    ) -> int:
        """追加一个版本。自动计算 version 序号（per content_type，max+1）。返回新版本号。"""
        if self._db is None:
            return 0
        # 计算下一个版本号 — 必须加 content_type 过滤
        docs = await self._db.find(
            COLLECTION,
            {"session_id": session_id, "content_type": content_type},
            sort=[("version", -1)],
            limit=1,
        )
        next_version = (docs[0]["version"] + 1) if docs else 1
        doc = {
            "session_id": session_id,
            "content_type": content_type,
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
        content_type: str = "l2",
    ) -> list[VersionEntry]:
        """分页查询版本历史。默认返回最近 20 条。"""
        if self._db is None:
            return []
        # 兼容旧文档（无 content_type 字段）— 当作 l2
        type_filter: dict = (
            {"content_type": content_type}
            if content_type != "l2"
            else {"$or": [{"content_type": "l2"}, {"content_type": {"$exists": False}}]}
        )
        filter_expr: dict = {"session_id": session_id, **type_filter}
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
                content_type=doc.get("content_type", "l2"),
            )
            for doc in docs
        ]

    async def get_version(
        self, session_id: SessionId, version: int,
        content_type: str = "l2",
    ) -> VersionEntry | None:
        """获取指定版本。"""
        if self._db is None:
            return None
        type_filter: dict = (
            {"content_type": content_type}
            if content_type != "l2"
            else {"$or": [{"content_type": "l2"}, {"content_type": {"$exists": False}}]}
        )
        docs = await self._db.find(
            COLLECTION,
            {"session_id": session_id, "version": version, **type_filter},
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
            content_type=doc.get("content_type", "l2"),
        )

    async def count_versions(
        self, session_id: SessionId,
        content_type: str = "l2",
    ) -> int:
        """返回版本总数。"""
        if self._db is None:
            return 0
        type_filter: dict = (
            {"content_type": content_type}
            if content_type != "l2"
            else {"$or": [{"content_type": "l2"}, {"content_type": {"$exists": False}}]}
        )
        return await self._db.count(
            COLLECTION,
            {"session_id": session_id, **type_filter},
        )
