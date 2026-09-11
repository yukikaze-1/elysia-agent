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
        self._injected_db = db is not None

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
                "mongodb/default",
                base_url=self._base_url,
            )
            await self._ensure_version_index()
            logger.info("CharacterVersionRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("CharacterVersionRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def _ensure_version_index(self) -> None:
        """建立 `(session_id, content_type, version)` 索引，**可用性优先**（S8）。

        唯一性是数据完整性目标，但**不得**因为脏数据让整个 repo 不可用——原实现把
        「检测失败」当「无重复」→ 建唯一索引失败 → repo 降级，可恢复的数据完整性问题
        被伪装成可用性问题。

        本方法的三条纪律：

        1. **检测失败不等于无重复**：`aggregate` 失败时按「可能有重复」处理（降级非唯一）。
        2. **选项冲突时绝不重复调用 `create_index`**：Mongo 在「同名索引已存在但选项不同」
           时抛 `OperationFailure`（实测确认），该异常会被 `connect()` 的外层 `except`
           捕获并令整个 repo 不可用。故先 `list_indexes()` 比对，冲突则保留现状并告警。
        3. **降级不自我修复**：一旦建成非唯一索引，后续即使数据变干净也不会自动恢复唯一性
           ——必须先 `drop_index()` 再重启。告警中给出具体命令。
        """
        assert self._db is not None  # 调用方保证已连接
        index_name = "session_id_1_content_type_1_version_-1"

        desired_unique, dup_sample = await self._probe_duplicates()

        existing: dict[object, dict] = {
            idx.get("name"): idx for idx in await self._db.list_indexes(COLLECTION)
        }
        current = existing.get(index_name)

        if current is not None and bool(current.get("unique", False)) != desired_unique:
            # 选项冲突：保留现有索引，绝不在此调用 create_index（见纪律 2）
            logger.warning(
                "\n"
                "==================================================================\n"
                "  CharacterVersionRepository：索引选项冲突，保留现有索引\n"
                "    索引  : %s\n"
                "    现状  : unique=%s    期望: unique=%s\n"
                "  未重建索引（同名不同选项会抛 OperationFailure 并令整个 repo 不可用）。\n"
                "  若需恢复唯一约束（当前期望唯一却被降级）：\n"
                "    1) 先清理重复文档；2) drop_index('%s', '%s')；3) 重启\n"
                "==================================================================",
                index_name, bool(current.get("unique", False)), desired_unique,
                COLLECTION, index_name,
            )
            return

        if not desired_unique:
            logger.warning(
                "\n"
                "==================================================================\n"
                "  CharacterVersionRepository：`(session_id, content_type, version)` 唯一性**未强制**\n"
                "    原因  : %s\n"
                "    影响  : 同一会话同一 content_type 可能出现重复版本号（L2/C6 版本历史被污染）\n"
                "    处置  : 清理重复文档后 drop_index('%s', '%s') 并重启，即可恢复唯一约束\n"
                "    （降级不会自我修复：唯一索引一旦降级为非唯一，后续启动不会再升级回来）\n"
                "==================================================================",
                dup_sample or "重复检测失败（无法确认是否已有重复）",
                COLLECTION, index_name,
            )

        await self._db.ensure_index(COLLECTION, [
            ("session_id", 1), ("content_type", 1), ("version", -1),
        ], unique=desired_unique)

    async def _probe_duplicates(self) -> tuple[bool, str]:
        """检测重复版本号。

        Returns:
            (是否可建唯一索引, 人类可读的重复样本/失败原因)。检测失败时返回
            (False, 原因)——**不**把「查不到」当作「无重复」（S8 纪律 1）。
        """
        assert self._db is not None
        pipeline = [
            {"$group": {
                "_id": {"sid": "$session_id", "ct": "$content_type", "v": "$version"},
                "count": {"$sum": 1},
            }},
            {"$match": {"count": {"$gt": 1}}},
        ]
        try:
            dup_docs = list(await self._db.aggregate(COLLECTION, pipeline))
        except Exception as e:
            return False, f"重复检测失败：{type(e).__name__}: {e}"
        if not dup_docs:
            return True, ""
        sample = "; ".join(
            f"session_id={d.get('_id', {}).get('sid')} "
            f"content_type={d.get('_id', {}).get('ct')} "
            f"version={d.get('_id', {}).get('v')} ×{d.get('count')}"
            for d in dup_docs[:5]
        )
        more = f"（共 {len(dup_docs)} 组重复，此处列前 5）" if len(dup_docs) > 5 else ""
        return False, f"发现 {len(dup_docs)} 组重复版本号：{sample}{more}"

    async def close(self) -> None:
        """关闭数据库连接，释放资源。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        """健康检查：真实 ping（P1-05），失败时重连。

        Returns:
            True 表示可用，False 表示不可用。
        """
        return await self._health_check()

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
