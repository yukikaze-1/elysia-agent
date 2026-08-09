"""AttachmentRepository — MongoDB attachments 集合的 Repository 实现。

批次 7（BE-070）：附件上传记录 + 引用计数 + 软删状态的唯一事实来源。

与 GenerationRepository 的关键差异（spec §3.1，fail-closed）：Mongo 未连接
（离线）时**所有**附件读写操作 raise StorageUnavailableError——不做静默 no-op。
附件上传写盘成功但无记录会造成「文件孤儿 + 消息校验失败」的不可解释失败，
故离线即抛错，由调用方返回 503 STORAGE_UNAVAILABLE。

文档 schema（批次 7 spec §3.1）：
    {
        attachment_id,    # att_ 前缀，唯一（_id 用 attachment_id 语义，复用 _new_id）
        kind,             # image / video / audio / file（sticker 非附件，内置资源）
        mime,             # filetype 真值判定结果
        size,             # 字节
        filename,         # 原始文件名（脱敏，仅展示用）
        stored_name,      # 落盘文件名 uuid.ext
        sha256,           # 内容哈希
        ref_count,        # 引用计数（消息引用次数）
        refs,             # 来源消息 message_id 列表（含归档消息）
        status,           # active / deleted（软删标记）
        created_at,       # 建档时间（UTC ISO 8601）
        deleted_at,       # 软删时间（GC 保留期依据）
    }

索引：attachment_id unique + status + ref_count + created_at。TTL 不设——
附件生命周期由 GC 管（spec §5.4：软删保留期 + 物理删，不走 TTL）。
"""

import logging

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema.types import AttachmentId
from core.schema.utils import now_iso
from core.storage.base import Repository, StorageUnavailableError

logger = logging.getLogger(__name__)

COLLECTION = "attachments"

# 附件状态常量
STATUS_ACTIVE = "active"
STATUS_DELETED = "deleted"

# 允许的 kind（spec §3.2：附件四类；sticker 非附件，内置资源）
KINDS = ("image", "video", "audio", "file")

# 完整分页页大小（P0-01）：按唯一 attachment_id keyset 分页，规避 Mongo 默认 limit=100
_PAGE_SIZE = 500

# StorageUnavailableError 自 core.storage.base 收口（B1）——附件操作 fail-closed 的
# 统一 typed 异常；旧导入经此 re-export 保持兼容。


class AttachmentRepository(Repository):
    """附件记录存储 — MongoDB attachments 集合（fail-closed）。"""

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 database_name: str = "default",
                 base_url: str | None = None):
        """初始化附件存储。

        Args:
            db: 可选的文档数据库后端，为 None 时需调用 connect() 建立连接。
            database_name: MongoDB 数据库名称，默认 "default"。
            base_url: MongoDB 连接串，为 None 时从 MONGODB_URI 环境变量读取。
        """
        self._db: DocumentDBBackend | None = db
        self._database_name = database_name
        self._base_url = base_url
        self._injected_db = db is not None

    def _require_db(self) -> DocumentDBBackend:
        """fail-closed：未连接（离线）时 raise StorageUnavailableError。

        Returns:
            可用的 DocumentDBBackend。

        Raises:
            StorageUnavailableError: MongoDB 未连接。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "attachments 集合不可用：MongoDB 未连接（离线）")
        return self._db

    # ---- lifecycle ----

    async def connect(self) -> bool:
        """建立 MongoDB 连接，创建索引。

        TTL 不设——附件生命周期由 GC 管（软删保留期 + 物理删）。

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
            await self._db.ensure_index(COLLECTION, [("attachment_id", 1)], unique=True)
            await self._db.ensure_index(COLLECTION, [("status", 1)])
            await self._db.ensure_index(COLLECTION, [("ref_count", 1)])
            await self._db.ensure_index(COLLECTION, [("created_at", 1)])
            logger.info("AttachmentRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("AttachmentRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        """关闭数据库连接，释放资源。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        """健康检查：真实 ping（P1-05），失败时重连并重建索引。

        Returns:
            True 表示可用，False 表示不可用。
        """
        return await self._health_check()

    # ---- core operations ----

    async def create(self, doc: dict) -> None:
        """按 attachment_id upsert 一条附件文档。

        update_one(upsert=True)——上传落记录。doc 须含 attachment_id。

        Args:
            doc: 附件文档（spec §3.1 schema）。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线）。
            ValueError: doc 缺少 attachment_id。
        """
        db = self._require_db()
        attachment_id = doc.get("attachment_id")
        if not attachment_id:
            raise ValueError("attachment_id 必填")
        await db.update_one(
            COLLECTION, {"attachment_id": attachment_id}, {"$set": doc}, upsert=True)

    async def get(self, attachment_id: AttachmentId) -> dict | None:
        """按 attachment_id 取单条附件文档。

        Args:
            attachment_id: 附件 ID（att_ 前缀）。

        Returns:
            附件文档 dict，不存在返回 None。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线）。
        """
        db = self._require_db()
        results = await db.find(COLLECTION, {"attachment_id": attachment_id}, limit=1)
        return results[0] if results else None

    async def add_ref(self, attachment_id: AttachmentId, message_id: str) -> None:
        """幂等原子 add：仅当 refs 不含该 message_id 才 addToSet + inc。

        条件 update（$addToSet + $inc）替代读改写——并发多次 add_ref 不同
        message_id 不丢更新，ref_count==len(refs) 不变量恒成立。同 message_id
        重复关联不双计（$ne 过滤 + addToSet 幂等）。记录不存在时为 no-op
        （由 GC 规则 3 兜底）。

        Args:
            attachment_id: 附件 ID。
            message_id: 引用该附件的消息 ID。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线）。
        """
        db = self._require_db()
        await db.update_one(
            COLLECTION,
            {"attachment_id": attachment_id, "refs": {"$ne": message_id}},
            {"$addToSet": {"refs": message_id}, "$inc": {"ref_count": 1}},
        )

    async def remove_ref(self, attachment_id: AttachmentId, message_id: str) -> None:
        """原子 remove：pull + dec（仅当 refs 确实含该 message_id）；归零且 active → 软删。

        条件 update 替代读改写——首跳以 `"refs": message_id` 为过滤条件，仅当
        refs 确实含该 message_id 才 $pull + $inc(-1)，重复 remove 不会把
        ref_count 减成负数；归零且仍 active 才条件软删（ref_count==0 且
        status==active），已是 deleted 不重复写。记录不存在时为 no-op。

        Args:
            attachment_id: 附件 ID。
            message_id: 移除引用的消息 ID。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线）。
        """
        db = self._require_db()
        await db.update_one(
            COLLECTION,
            {"attachment_id": attachment_id, "refs": message_id},
            {"$pull": {"refs": message_id}, "$inc": {"ref_count": -1}},
        )
        await db.update_one(
            COLLECTION,
            {"attachment_id": attachment_id, "ref_count": 0, "status": STATUS_ACTIVE},
            {"$set": {"status": "deleted", "deleted_at": now_iso()}},
        )

    async def mark_deleted(self, attachment_id: AttachmentId) -> None:
        """软删标记：status=deleted + deleted_at=now（文件保留，GC 延后物理删）。

        Args:
            attachment_id: 附件 ID。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线）。
        """
        db = self._require_db()
        await db.update_one(
            COLLECTION, {"attachment_id": attachment_id},
            {"$set": {"status": STATUS_DELETED, "deleted_at": now_iso()}})

    async def delete_record(self, attachment_id: AttachmentId) -> None:
        """物理删除附件记录（GC 规则 1 用：软删记录直接删记录，再删文件）。

        注意：DocumentDBBackend 抽象无 delete_one，attachment_id 唯一，
        delete_many 等价 delete_one。

        Args:
            attachment_id: 附件 ID。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线）。
        """
        db = self._require_db()
        await db.delete_many(COLLECTION, {"attachment_id": attachment_id})

    async def _find_all(self, collection: str, filter_expr: dict) -> list[dict]:
        """完整分页读取所有匹配文档（P0-01：keyset cursor by attachment_id）。

        底层 find 默认 limit=100 —— GC 若直接依赖会截断第 101+ 条，把仍有效的
        附件误判为磁盘孤儿。这里按唯一 attachment_id 升序做 keyset 分页，直到
        读完或结果不足一页；任一分页失败包装为 StorageUnavailableError，
        绝不把部分结果当完整结果交给删除阶段（fail-closed）。

        Args:
            collection: 目标 collection 名称。
            filter_expr: 查询过滤条件。

        Returns:
            全部匹配文档列表（attachment_id 升序）。

        Raises:
            StorageUnavailableError: MongoDB 未连接或任一页查询失败。
        """
        db = self._require_db()
        out: list[dict] = []
        last: str | None = None
        while True:
            cond = dict(filter_expr)
            if last is not None:
                cond["attachment_id"] = {"$gt": last}
            try:
                page = await db.find(
                    collection, cond,
                    sort=[("attachment_id", 1)], limit=_PAGE_SIZE)
            except Exception as e:
                raise StorageUnavailableError(
                    f"附件完整分页失败（{collection}）：{e}") from e
            if not page:
                break
            out.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            last = page[-1].get("attachment_id")
            if not last:
                # 防御：attachment_id 为唯一索引键，正常必有；无键无法继续 keyset。
                raise StorageUnavailableError(
                    "附件分页遇到缺少 attachment_id 的记录，中止完整读取")
        return out

    async def find_by_status(self, status: str) -> list[dict]:
        """按状态查询全部附件（完整分页，P0-01：不依赖默认 limit=100）。

        Args:
            status: active / deleted。

        Returns:
            匹配状态的完整附件文档列表。

        Raises:
            StorageUnavailableError: MongoDB 未连接或分页中途失败。
        """
        return await self._find_all(COLLECTION, {"status": status})

    async def find_unreferenced_older_than(self, iso: str) -> list[dict]:
        """未关联（ref_count==0）且 created_at < iso 的全部附件（GC 规则 3：7 天未关联回收）。

        完整分页（P0-01）——候选可能超过 100 条，截断会让超期候选漏回收。

        Args:
            iso: 过期阈值（ISO UTC），created_at 严格小于才回收。

        Returns:
            完整候选附件文档列表。

        Raises:
            StorageUnavailableError: MongoDB 未连接或分页中途失败。
        """
        return await self._find_all(
            COLLECTION, {"ref_count": 0, "created_at": {"$lt": iso}})

    async def find_by_stored_name(self, kind: str, stored_name: str) -> dict | None:
        """按 (kind, stored_name) 查单条附件（GC 规则 4 逐文件复核用）。

        磁盘 orphan 候选 unlink/隔离前，用它复核数据库是否已存在对应记录，
        闭合扫描期间新建引用/记录窗口（P0-01）。

        Args:
            kind: 附件 kind。
            stored_name: 落盘文件名。

        Returns:
            匹配附件文档，不存在返回 None。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线）。
        """
        db = self._require_db()
        results = await db.find(
            COLLECTION, {"kind": kind, "stored_name": stored_name}, limit=1)
        return results[0] if results else None
