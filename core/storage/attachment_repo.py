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
from core.schema.utils import now_iso
from core.storage.base import Repository

logger = logging.getLogger(__name__)

COLLECTION = "attachments"

# 附件状态常量
STATUS_ACTIVE = "active"
STATUS_DELETED = "deleted"

# 允许的 kind（spec §3.2：附件四类；sticker 非附件，内置资源）
KINDS = ("image", "video", "audio", "file")


class StorageUnavailableError(RuntimeError):
    """附件存储不可用（MongoDB 未连接）。

    附件相关操作 fail-closed——离线时 raise，由调用方映射为 503 STORAGE_UNAVAILABLE。
    """


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
            await self._db.ensure_index(COLLECTION, [("attachment_id", 1)], unique=True)
            await self._db.ensure_index(COLLECTION, [("status", 1)])
            await self._db.ensure_index(COLLECTION, [("ref_count", 1)])
            await self._db.ensure_index(COLLECTION, [("created_at", 1)])
            logger.info("AttachmentRepository: MongoDB 已恢复连接")
            return True
        except Exception:
            return False

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

    async def get(self, attachment_id: str) -> dict | None:
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

    async def add_ref(self, attachment_id: str, message_id: str) -> None:
        """消息关联：refs 加 message_id（幂等，至多入一次），ref_count 同步。

        读改写（$set）维护 ref_count==len(refs) 不变量；同 message_id 重复关联
        幂等去重（发送重试不双计）。记录不存在时为 no-op（由 GC 规则 3 兜底）。

        Args:
            attachment_id: 附件 ID。
            message_id: 引用该附件的消息 ID。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线）。
        """
        db = self._require_db()
        doc = await self.get(attachment_id)
        if doc is None:
            return
        refs = list(doc.get("refs") or [])
        if message_id not in refs:
            refs.append(message_id)
        await db.update_one(
            COLLECTION, {"attachment_id": attachment_id},
            {"$set": {"refs": refs, "ref_count": len(refs)}})

    async def remove_ref(self, attachment_id: str, message_id: str) -> None:
        """消息移除：refs 移除 message_id，ref_count 递减。

        读改写（$set）维护 ref_count==len(refs) 不变量。refs 归零且由「消息移除」
        归零 → status=deleted + deleted_at=now（spec §3.1，文件保留 GC 延后删）。
        记录不存在时为 no-op。

        Args:
            attachment_id: 附件 ID。
            message_id: 移除引用的消息 ID。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线）。
        """
        db = self._require_db()
        doc = await self.get(attachment_id)
        if doc is None:
            return
        refs = [r for r in (doc.get("refs") or []) if r != message_id]
        update: dict = {"refs": refs, "ref_count": len(refs)}
        if len(refs) == 0:
            # ref_count 由消息移除归零 → 软删（I2，§5.3）
            update["status"] = STATUS_DELETED
            update["deleted_at"] = now_iso()
        await db.update_one(
            COLLECTION, {"attachment_id": attachment_id}, {"$set": update})

    async def mark_deleted(self, attachment_id: str) -> None:
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

    async def delete_record(self, attachment_id: str) -> None:
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

    async def find_by_status(self, status: str) -> list[dict]:
        """按状态查询附件（如软删扫描）。

        Args:
            status: active / deleted。

        Returns:
            匹配状态的附件文档列表。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线）。
        """
        db = self._require_db()
        return await db.find(COLLECTION, {"status": status})

    async def find_unreferenced_older_than(self, iso: str) -> list[dict]:
        """未关联（ref_count==0）且 created_at < iso 的附件（GC 规则 3：7 天未关联回收）。

        Args:
            iso: 过期阈值（ISO UTC），created_at 严格小于才回收。

        Returns:
            候选附件文档列表。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线）。
        """
        db = self._require_db()
        return await db.find(
            COLLECTION, {"ref_count": 0, "created_at": {"$lt": iso}})
