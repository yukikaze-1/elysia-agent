"""CharacterVersionRepository — MongoDB character_versions 集合。"""
import logging
from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema.types import SessionId
from core.schema.compression import CompressionTrigger, VersionEntry
from core.storage.base import Repository

if TYPE_CHECKING:
    from core.schema.types import IsoTimestamp

logger = logging.getLogger(__name__)
COLLECTION = "character_versions"       # 改名（原 character_l2_versions）


class CharacterVersionRepository(Repository):
    """角色 Section 版本历史存储 — append-only, 永久保留。
    支持多种 content_type: "l2", "c6", 未来扩展 "c7", "c2" 等。"""

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 base_url: str | None = None):
        """初始化角色版本存储。

        Args:
            db: 可选的文档数据库后端，为 None 时需调用 connect() 建立连接。
            base_url: MongoDB 连接串，为 None 时从 MONGODB_URI 环境变量读取。
        """
        self._db: DocumentDBBackend | None = db
        self._base_url = base_url

    # ---- lifecycle ----

    async def connect(self) -> bool:
        """建立 MongoDB 连接，创建索引。

        Returns:
            True 表示连接成功，False 表示连接失败。
        """
        try:
            self._db = DocumentDBFactory.create(
                "mongodb/default",
                base_url=self._base_url,
            )
            # 检查脏数据（重复文档），有重复时降级为非唯一索引
            dup_pipeline = [
                {"$group": {
                    "_id": {"sid": "$session_id", "ct": "$content_type", "v": "$version"},
                    "count": {"$sum": 1},
                }},
                {"$match": {"count": {"$gt": 1}}},
            ]
            try:
                dup_docs = await self._db.aggregate(COLLECTION, dup_pipeline)
                unique = len(list(dup_docs)) == 0
                if not unique:
                    logger.warning(
                        "CharacterVersionRepository: 发现重复文档，降级为非唯一索引")
            except Exception as e:
                # aggregate 失败（网络/API 不支持）时无法确认是否有重复——
                # 保守建非唯一索引，避免 ensure_index 失败拖垮整个 repo（S8）。
                # 原实现把失败当「无重复」→ 建唯一索引失败 → 整个 repo 降级不可用，
                # 可恢复的数据完整性问题被伪装成可用性问题。
                logger.warning(
                    "CharacterVersionRepository: 重复检测失败，保守降级为非唯一索引 (%s)",
                    e,
                )
                unique = False
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

    # ---- core API ----

    async def append_version(
        self, session_id: SessionId, text: str,
        timestamp: "IsoTimestamp", trigger: CompressionTrigger,
        change_summary: list[str],
        content_type: str = "l2",
    ) -> int:
        """追加一个版本。自动计算 version 序号（per content_type，max+1）。

        Args:
            session_id: 会话 ID。
            text: 版本文本内容。
            timestamp: ISO 格式时间戳。
            trigger: 压缩触发器类型。
            change_summary: 变更摘要列表。
            content_type: 内容类型标识，默认 "l2"。

        Returns:
            新版本号。若数据库不可用返回 0。
        """
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
        logger.info("CharacterVersionRepository append_version: session_id=%s, content_type=%s, version=%d, trigger=%s, text_len=%d",
                    session_id, content_type, next_version, trigger.value, len(text))
        await self._db.insert_one(COLLECTION, doc)
        return next_version

    async def list_versions(
        self, session_id: SessionId, *,
        limit: int = 20, before_version: int | None = None,
        content_type: str = "l2",
    ) -> list[VersionEntry]:
        """分页查询版本历史。

        Args:
            session_id: 会话 ID。
            limit: 返回条数上限，默认 20。
            before_version: 游标，只返回此版本号之前的记录。
            content_type: 内容类型标识，默认 "l2"。

        Returns:
            版本条目列表，按版本号降序排列。若数据库不可用返回空列表。
        """
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
        """获取指定版本。

        Args:
            session_id: 会话 ID。
            version: 版本号。
            content_type: 内容类型标识，默认 "l2"。

        Returns:
            版本条目，若不存在或数据库不可用返回 None。
        """
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
        """返回版本总数。

        Args:
            session_id: 会话 ID。
            content_type: 内容类型标识，默认 "l2"。

        Returns:
            版本总数。若数据库不可用返回 0。
        """
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
