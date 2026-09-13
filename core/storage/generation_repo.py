"""GenerationRepository — MongoDB generations 集合的 Repository 实现。

批次 3（BE-024）：持久化 generation 生命周期（running/completed/failed/
cancelled/interrupted）到 Mongo，重连 + 搜索可按 status 查询。

文档 schema：
    {
        generation_id,        # gen_ 前缀，唯一
        session_id,           # 目标会话 ID（D-m 归属）
        user_message_id,      # 触发生成的用户消息 ID
        message_id,           # assistant 父消息 ID（generation 开始预分配，落库一致）
        status,               # running / completed / failed / cancelled / interrupted
        model,                # 生成所用模型
        finish_reason,        # stop / error / None
        error,                # 错误信息（status=failed 时）
        created_at,           # 建档时间（ISO UTC）
        updated_at,           # 最后更新时间（ISO UTC，排序/展示用）
        ttl_expires_at,       # 真实 BSON Date（30 天 TTL 基准，B6/P2-01）
        finished_at,          # 终态落位时间（None 表示未终态）
    }

索引：generation_id unique + session_id+updated_at + message_id（find_by_message）+ ttl_expires_at TTL 30d。
"""

import base64
import logging

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema.types import GenerationId, MessageId, SessionId
from core.schema.utils import now_iso
from core.storage.base import Repository, StorageUnavailableError, bson_expires

logger = logging.getLogger(__name__)

COLLECTION = "generations"

# TTL：30 天（B6/P2-01：改按真实 BSON Date `ttl_expires_at` 过期——旧 updated_at
# 为 ISO 字符串，Mongo TTL 不生效，临时记录永不回收）
GENERATIONS_TTL_SECONDS = 30 * 86400

# generation 生命周期状态常量
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"


def _encode_cursor(created_at: str, generation_id: str) -> str:
    """编码 keyset 游标：base64url(`created_at|generation_id`)。"""
    raw = f"{created_at}|{generation_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """解码 keyset 游标 → (created_at, generation_id)。格式非法抛 ValueError。"""
    raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    created_at, _, gen_id = raw.partition("|")
    if not created_at or not gen_id:
        raise ValueError("游标格式非法")
    return created_at, gen_id


class GenerationRepository(Repository):
    """generation 生命周期存储 — MongoDB generations 集合。"""

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 database_name: str = "default",
                 base_url: str | None = None):
        """初始化 generation 存储。

        Args:
            db: 可选的文档数据库后端，为 None 时需调用 connect() 建立连接。
            database_name: MongoDB 数据库名称，默认 "default"。
            base_url: MongoDB 连接串，为 None 时从 MONGODB_URI 环境变量读取。
        """
        self._db: DocumentDBBackend | None = db
        self._database_name = database_name
        self._base_url = base_url
        self._injected_db = db is not None

    # ---- lifecycle ----

    async def connect(self) -> bool:
        """建立 MongoDB 连接，创建索引和 TTL。

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
            await self._db.ensure_index(COLLECTION, [("generation_id", 1)], unique=True)
            await self._db.ensure_index(COLLECTION, [("session_id", 1), ("updated_at", -1)])
            await self._db.ensure_index(COLLECTION, [("message_id", 1)])
            try:
                # B6/P2-01：TTL 基于真实 BSON Date ttl_expires_at（旧 updated_at ISO 不生效）
                await self._db.ensure_index(
                    COLLECTION, [("ttl_expires_at", 1)],
                    expireAfterSeconds=GENERATIONS_TTL_SECONDS)
            except Exception as e:
                logger.warning("GenerationRepository: TTL index failed (%s)", e)
            await self._backfill_ttl()
            logger.info("GenerationRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("GenerationRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def _backfill_ttl(self) -> None:
        """为缺 ttl_expires_at 的旧记录回填（B6/P2-01 迁移，幂等）。"""
        if self._db is None:
            return
        try:
            stale = await self._db.find(
                COLLECTION, {"ttl_expires_at": {"$exists": False}}, limit=500)
            for doc in stale:
                try:
                    await self._db.update_one(
                        COLLECTION,
                        {"generation_id": doc.get("generation_id")},
                        {"$set": {"ttl_expires_at": bson_expires(30)}})
                except Exception:
                    logger.debug("generation ttl 回填失败（幂等，下次重试）", exc_info=True)
        except Exception:
            logger.warning("generation ttl 回填查询失败", exc_info=True)

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

    async def upsert(self, doc: dict) -> None:
        """按 generation_id upsert 一条 generation 文档。

        update_one(upsert=True)——mark-before-register（先写终态后建档）仍能创建文档。
        未连接（db=None）时静默跳过（Mongo 离线降级）。

        Args:
            doc: generation 文档，须含 generation_id。
        """
        if self._db is None:
            return
        generation_id = doc.get("generation_id")
        if not generation_id:
            return
        # B6/P2-01：TTL 基准 BSON Date——新写记录补 ttl_expires_at
        doc.setdefault("ttl_expires_at", bson_expires(30))
        await self._db.update_one(
            COLLECTION, {"generation_id": generation_id}, {"$set": doc}, upsert=True)

    async def find_one(self, generation_id: GenerationId) -> dict | None:
        """按 generation_id 取单条文档。不存在或数据库不可用返回 None。"""
        if self._db is None:
            return None
        results = await self._db.find(COLLECTION, {"generation_id": generation_id}, limit=1)
        return results[0] if results else None

    async def find_by_message(self, message_id: MessageId) -> dict | None:
        """按 assistant 父消息 ID 反查最近一条 generation（D-q 搜索用）。

        message_id 为 assistant 消息 ID（generation 开始预分配，落库一致）。
        按 created_at 降序取最新一条。

        Args:
            message_id: assistant 父消息 ID。

        Returns:
            最近的 generation 文档，不存在或数据库不可用返回 None。
        """
        if self._db is None:
            return None
        results = await self._db.find(
            COLLECTION, {"message_id": message_id},
            sort=[("created_at", -1)], limit=1)
        return results[0] if results else None

    async def find_by_user_message(self, user_message_id: MessageId) -> dict | None:
        """按 user_message_id 反查最近一条 generation（幂等 commit 证据，Task 3/4）。

        幂等恢复：恢复 stale CLAIMED 记录时查「该用户消息是否已生成过」——存在
        generation 记录即视为已处理（commit 证据）。user_message_id 为触发生成的
        用户消息 ID（register 建档时写入），按 created_at 降序取最新一条。

        Args:
            user_message_id: 触发生成的用户消息 ID。

        Returns:
            最近的 generation 文档；**不存在返回 None**。

        Raises:
            StorageUnavailableError: 数据库不可用 / 查询失败（S2-B6）。

        **三态而不是两态**：本方法是幂等恢复的**提交证据**探测——"查不到"与"查不了"
        必须分开。原实现 `_db is None → return None`，与"真的没有 generation"同形，
        调用方据此走 TAKEOVER 重新处理 → **同一 clientRequestId 产生两条 assistant
        回复 + user_message 归档两次 + token 翻倍**，正是 D6c 幂等机制要消除的问题。
        （对照：`find_by_message` 是搜索 API，失败返回 None 属正常降级，不受此约束。）
        """
        if self._db is None:
            raise StorageUnavailableError(
                "GenerationRepository 未连接——无法判定提交证据（不得当作不存在）")
        results = await self._db.find(
            COLLECTION, {"user_message_id": user_message_id},
            sort=[("created_at", -1)], limit=1)
        return results[0] if results else None

    async def find_by_session(self, session_id: SessionId, *, status: str | None = None,
                              cursor: str | None = None,
                              limit: int = 20) -> tuple[list[dict], bool, str | None]:
        """按 session 键集分页查询 generation（D-q 搜索用）。

        排序 created_at DESC, generation_id DESC；keyset 游标
        base64(`created_at|generation_id`)，谓词同 §5.3 模式：
        `(created_at < C) OR (created_at = C AND generation_id < G)`。

        Args:
            session_id: 目标会话 ID。
            status: 可选状态过滤（running/completed/failed/cancelled/interrupted）。
            cursor: keyset 游标（base64）。
            limit: 每页条数，默认 20。

        Returns:
            (docs, has_more, next_cursor) 元组。数据库不可用或游标非法返回
            ([], False, None)。
        """
        if self._db is None:
            return [], False, None
        filter_expr: dict = {"session_id": session_id}
        if status is not None:
            filter_expr["status"] = status
        if cursor:
            try:
                created_at, gen_id = _decode_cursor(cursor)
            except Exception:
                return [], False, None
            filter_expr["$or"] = [
                {"created_at": {"$lt": created_at}},
                {"created_at": created_at, "generation_id": {"$lt": gen_id}},
            ]
        docs = await self._db.find(
            COLLECTION, filter_expr,
            sort=[("created_at", -1), ("generation_id", -1)], limit=limit + 1)
        has_more = len(docs) > limit
        if has_more:
            docs = docs[:limit]
        next_cursor: str | None = None
        if has_more and docs:
            last = docs[-1]
            next_cursor = _encode_cursor(
                last.get("created_at", ""), last.get("generation_id", ""))
        return docs, has_more, next_cursor

    async def delete_by_session(self, session_id: SessionId) -> int:
        """按会话删除 generation 生命周期记录。

        与 `MessageRepository.delete_by_session` / `TokenRepository.delete_by_session`
        同语义：会话级收尾（删除会话、清理联调夹具）需要把该会话派生出的记录一并清掉，
        否则留下指向已删会话的孤儿行（generations 有 30 天 TTL，期间一直可见）。

        Args:
            session_id: 会话 ID。

        Returns:
            删除的记录数（0 = 无记录，幂等）。

        Raises:
            StorageUnavailableError: MongoDB 未连接——离线删除不得静默返回 0
                被 caller 当成功（同 Message/Token 两个 delete_by_session）。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "GenerationRepository 未连接（MongoDB 不可用），generation 记录未删除")
        removed = await self._db.delete_many(COLLECTION, {"session_id": session_id})
        logger.info(
            "GenerationRepository delete_by_session: session_id=%s, deleted=%d",
            session_id, removed)
        return removed

    async def mark_stale(self, status_from: str, older_than_iso: str) -> int:
        """把 status_from 且 updated_at < older_than_iso 的 generation 标为 interrupted。

        崩溃残留清理：旧 running 超过 cutoff 视为中断。Mongo 后端无 update_many
        抽象——find + 逐条 update_one 实现。

        Args:
            status_from: 待清理的旧状态（通常 "running"）。
            older_than_iso: 过期阈值（ISO UTC），updated_at 严格小于才清理。

        Returns:
            修改的文档数。数据库不可用返回 0。
        """
        if self._db is None:
            return 0
        stale_docs = await self._db.find(
            COLLECTION,
            {"status": status_from, "updated_at": {"$lt": older_than_iso}},
        )
        modified = 0
        for doc in stale_docs:
            gen_id = doc.get("generation_id")
            if not gen_id:
                continue
            await self._db.update_one(
                COLLECTION, {"generation_id": gen_id},
                {"$set": {
                    "status": STATUS_INTERRUPTED,
                    "updated_at": now_iso(),
                    # review 修复（批次 C）：终态语义与 mark() 一致——stale 清理标
                    # interrupted 一并写 finished_at（此前保持 None，契约
                    # finishedAt 恒空，无法区分中断时刻）。
                    "finished_at": now_iso(),
                }},
            )
            modified += 1
        if modified:
            logger.info(
                "GenerationRepository mark_stale: %d → interrupted (from=%s)",
                modified, status_from,
            )
        return modified
