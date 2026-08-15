"""IdempotencyRepository — MongoDB idempotency 集合（D6c 跨重启持久化）。

批次 2 起的 v2 幂等表是进程内 dict + TTL，重启丢失——ACK 丢失 + 后端重启后
Electron 重试可能重复生成。D6c 持久化到 Mongo：

文档 schema：
    {
        conversation_id,      # 目标会话
        client_request_id,    # 客户端请求 ID
        content_digest,       # 内容摘要（SHA-256 hex）
        message_id,           # 预分配用户消息 ID（ACK 与落库一致）
        status,               # claimed / accepted / rejected（三态；legacy in_progress 视同 claimed）
        claimed_at,           # 墙钟 unix 秒（过期 CLAIMED 接管依据；只允许 time.time()）
        expires_at,           # BSON Date——TTL 索引 expireAfterSeconds=0 到期即删
        received_at?,         # accepted/rejected ACK 重放负载（完整负载持久化）
        client_message_id?,   # ...
        conversation_id_wire?,# ...
        error?,               # rejected ACK 的 error 负载（code/message/retryable/...）
    }

索引：
- {conversation_id, client_request_id} 唯一——并发同键双写被唯一索引兜底
  （claim 的 find→insert 非原子，但唯一索引保证不重复；后写者读回既有记录）。
- {expires_at} TTL（expireAfterSeconds=0）——BSON Date 真到期删除，不用字符串。

所有权语义（D6/D6-d）：`claim()` 返回 `ClaimResult{entry, acquired}`——acquired=True
为新插入或过期 CLAIMED 接管（调用方可进入业务处理）；False 为既有记录（并发 loser/
重放，恒不进入业务处理）。并发同键的 DuplicateKey 经 `_call(non_fatal=...)` 裸抛原始
异常、不判死 repo（DuplicateKey 是业务冲突而非连接失败）。

fail-closed 语义由调用方决定（store 产品模式 503 / dev 内存降级）；本 repo 在
Mongo 离线时一律 raise StorageUnavailableError（对齐附件 repo 先例）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.storage.base import Repository, StorageUnavailableError

logger = logging.getLogger(__name__)

COLLECTION = "idempotency"

# 状态常量（幂等记录三态生命周期；legacy "in_progress"/"completed" 在
# IdempotencyEntry.from_doc 防御映射——本层 claim() 读原始 doc 时显式归一 in_progress）
STATUS_CLAIMED = "claimed"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"

# CLAIMED 新鲜窗口（unix 秒）：过期 CLAIMED 才允许接管。单源下沉在本模块（2026-08-14
# 模块化修复——消除 storage→input 反向依赖）；core.storage.idempotency 同包直接引用，
# ws_handler 的 IN_PROGRESS retryAfterMs 亦派生自本值。
GRACE_SECONDS = 30.0


def _grace_seconds() -> float:
    """CLAIMED 新鲜窗口（unix 秒），单源本模块常量 :data:`GRACE_SECONDS`。

    原单源在 core.storage.idempotency；迁移后同包直接复用，
    2026-08-14 下沉到本模块。input 层经 core.storage.idempotency 直接 import。
    """
    return GRACE_SECONDS


def _is_stale(claimed_at: float) -> bool:
    """claimed_at 是否过期（墙钟 time.time()，unix 秒）。

    只允许墙钟相减——monotonic 与墙钟相减为巨大负数，stale 判定永不触发
    （Global Constraints 硬性要求；跨重启可比）。
    """
    return time.time() - claimed_at > _grace_seconds()


def _is_duplicate_key(exc: BaseException) -> bool:
    """是否唯一索引冲突（并发同键双写）。驱动无关：检查异常类名（含 cause 链）。

    ``Repository._call`` 在 ``non_fatal`` 命中时裸抛原始异常（不包装）——raw driver
    异常的 ``__cause__`` 为 None，只查 cause 会漏判（review 复核确认），故同时检查
    ``type(exc).__name__`` 与 ``type(exc.__cause__).__name__``。
    """
    if getattr(type(exc), "__name__", "") == "DuplicateKeyError":
        return True
    cause = getattr(exc, "__cause__", None)
    return getattr(type(cause), "__name__", "") == "DuplicateKeyError"


@dataclass
class ClaimResult:
    """claim 所有权结果（D6）。

    entry: 实际生效的记录（新插入 doc / 既有记录 / 接管后更新 timestamps 的记录）。
    acquired: True = 新插入或过期 CLAIMED 接管（调用方可进入业务处理）；
              False = 已有记录（并发 loser/重放），恒不进入业务处理。
    """

    entry: dict
    acquired: bool


class IdempotencyRepository(Repository):
    """幂等 claim 存储 — MongoDB idempotency 集合（D6c）。"""

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 database_name: str = "default",
                 base_url: str | None = None):
        """初始化幂等存储。

        Args:
            db: 可选的文档数据库后端，为 None 时需调用 connect() 建立连接。
            database_name: MongoDB 数据库名称，默认 "default"。
            base_url: MongoDB 连接串，为 None 时从 MONGODB_URI 环境变量读取。
        """
        self._db: DocumentDBBackend | None = db
        self._injected_db = db is not None
        self._database_name = database_name
        self._base_url = base_url

    def _require_db(self) -> DocumentDBBackend:
        """取后端；未连接（离线）→ StorageUnavailableError（fail-closed）。"""
        if self._db is None:
            raise StorageUnavailableError("IdempotencyRepository 未连接（MongoDB 不可用）")
        return self._db

    async def connect(self) -> bool:
        """建立连接并建索引（唯一 + TTL）。

        Returns:
            True 表示连接成功，False 表示失败。
        """
        if self._db is None:
            try:
                self._db = DocumentDBFactory.create(
                    f"mongodb/{self._database_name}",
                    base_url=self._base_url,
                )
            except Exception as e:
                logger.warning("IdempotencyRepository 建连失败: %s", e)
                self._db = None
                return False
        try:
            await self.ensure_indexes()
            logger.info("IdempotencyRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("IdempotencyRepository 索引创建失败: %s", e)
            if not self._injected_db:
                self._db = None
            return False

    async def ensure_indexes(self) -> None:
        """建唯一索引（防并发双写）+ TTL 索引（expires_at 到期即删）。

        create_index 幂等（已存在则 no-op）；connect 与测试均可直接调用。
        """
        db = self._db
        if db is None:
            return
        await db.ensure_index(
            COLLECTION,
            [("conversation_id", 1), ("client_request_id", 1)],
            unique=True,
        )
        await db.ensure_index(COLLECTION, [("expires_at", 1)], expireAfterSeconds=0)

    async def close(self) -> None:
        """关闭连接，释放资源。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        """真实健康探测（P1-05）。"""
        return await self._health_check()

    # ---- core operations ----

    async def claim(self, conversation_id: str, client_request_id: str, *,
                    content_digest: str, message_id: str, status: str,
                    claimed_at: float | None = None,
                    expires_at: object) -> ClaimResult:
        """原子 claim，返回所有权结果（D6/D6-d）。

        find → insert（唯一索引兜底并发）：并发同键双写时后写者收到 DuplicateKeyError，
        经 ``_call(non_fatal=_is_duplicate_key)`` 裸抛原始异常、不判死 repo，随后读回
        既有记录返回 ``acquired=False``——不重复 generation（调用方用既有 message_id
        重放 ACK）。既有记录为过期 CLAIMED（claimed_at 超过 GRACE_SECONDS）→ 接管：
        update_one 延长 claimed_at/expires_at 后返回 ``acquired=True``。

        Args:
            conversation_id: 目标会话 ID。
            client_request_id: 客户端请求 ID。
            content_digest: 内容摘要。
            message_id: 预分配用户消息 ID。
            status: 状态（claimed / accepted / rejected；legacy in_progress 视同 claimed）。
            claimed_at: 墙钟 unix 秒（time.time()）；缺省取当前时刻。
            expires_at: BSON Date 过期时间（TTL 索引到期删除）。

        Returns:
            ClaimResult{entry, acquired}：acquired=True 表示获得所有权（新插入或过期
            接管），调用方方可进入业务处理；False 表示既有记录（并发 loser/重放），
            恒不进入业务处理。

        Raises:
            StorageUnavailableError: MongoDB 未连接/操作失败。DuplicateKey 业务冲突
                除外——不判死、裸抛原始异常后按 winner 分支返回 acquired=False。
        """
        db = self._require_db()
        if claimed_at is None:
            claimed_at = time.time()
        filter_expr = {
            "conversation_id": conversation_id,
            "client_request_id": client_request_id,
        }
        existing = await self._call(
            lambda: db.find(COLLECTION, filter_expr, limit=1),
            reason="IdempotencyRepository.claim 查询失败",
        )
        if existing:
            rec = existing[0]
            # legacy "in_progress" 与 STATUS_CLAIMED 一视同仁（防御映射同源——from_doc
            # 映射在 Task 2，但 claim() 读原始 doc，此处须显式归一，否则历史 CLAIMED
            # 永不接管）
            status_in_doc = "claimed" if rec.get("status") == "in_progress" else rec.get("status")
            if status_in_doc == STATUS_CLAIMED and _is_stale(rec.get("claimed_at", 0)):
                # 条件接管：原子性来自条件 update_one 的返回 modified_count，而非 find
                # 快照——并发双读同一 stale CLAIMED 文档时只有先写者赢，后写者因
                # claimed_at 已被刷新而 conditional filter 失配（modified_count=0）。
                # $or 的 missing-claimed_at 分支覆盖 pre-migration 文档（无 claimed_at
                # 字段 = 视为 stale）；当前写入恒带 claimed_at，新鲜值天然不匹配 $lt，
                # 防双接管（review Important#1）。
                cutoff = time.time() - _grace_seconds()
                win = await self._call(
                    lambda: db.update_one(
                        COLLECTION,
                        {
                            "conversation_id": conversation_id,
                            "client_request_id": client_request_id,
                            "status": {"$in": [STATUS_CLAIMED, "in_progress"]},
                            "$or": [{"claimed_at": {"$lt": cutoff}},
                                    {"claimed_at": {"$exists": False}}],
                        },
                        {"$set": {"claimed_at": claimed_at, "expires_at": expires_at}},
                    ),
                    reason="IdempotencyRepository.claim 接管更新失败",
                )
                if win:  # modified_count > 0 = 本次取得接管权
                    rec = {**rec, "claimed_at": claimed_at, "expires_at": expires_at}
                    return ClaimResult(entry=rec, acquired=True)
                # 竞态输家：重新读取，返回 acquired=False（读到 winner 更新后的新鲜 CLAIMED）
                existing = await self._call(
                    lambda: db.find(COLLECTION, filter_expr, limit=1),
                    reason="IdempotencyRepository.claim 接管竞态读回失败",
                )
                return ClaimResult(entry=existing[0], acquired=False)
            return ClaimResult(entry=rec, acquired=False)
        doc = {
            "conversation_id": conversation_id,
            "client_request_id": client_request_id,
            "content_digest": content_digest,
            "message_id": message_id,
            "status": status,
            "claimed_at": claimed_at,
            "expires_at": expires_at,
        }
        try:
            await self._call(
                lambda: db.insert_one(COLLECTION, dict(doc)),
                reason="IdempotencyRepository.claim 写入失败",
                non_fatal=_is_duplicate_key,
            )
        except Exception as e:
            if not _is_duplicate_key(e):
                raise
            # 并发同键双写 → 读回既有记录（唯一索引保证单记录；DuplicateKey 不判死）
            existing = await self._call(
                lambda: db.find(COLLECTION, filter_expr, limit=1),
                reason="IdempotencyRepository.claim 冲突读回失败",
            )
            if existing:
                return ClaimResult(entry=existing[0], acquired=False)
            raise  # 极罕见：索引冲突但记录已 TTL 删除 → fail-closed
        return ClaimResult(entry=doc, acquired=True)

    async def get(self, conversation_id: str, client_request_id: str) -> dict | None:
        """按 (conversation_id, client_request_id) 取单条记录；不存在返回 None。"""
        db = self._require_db()
        results = await self._call(
            lambda: db.find(
                COLLECTION,
                {"conversation_id": conversation_id,
                 "client_request_id": client_request_id},
                limit=1,
            ),
            reason="IdempotencyRepository.get 查询失败",
        )
        return results[0] if results else None

    async def complete(self, conversation_id: str, client_request_id: str, *,
                       status: str = STATUS_ACCEPTED,
                       payload: dict | None = None) -> None:
        """标记 claim 终态（accepted ACK 后调用；幂等）。

        payload 为完整 ACK 重放负载（received_at/client_message_id/
        conversation_id_wire 等），与 status 一并 ``$set`` 落库——重放可逐字重建。

        Args:
            conversation_id: 目标会话 ID。
            client_request_id: 客户端请求 ID。
            status: 终态（默认 accepted）。
            payload: 附加 ACK 负载字段（合并进 ``$set``）。
        """
        db = self._require_db()
        set_expr: dict = dict(payload) if payload else {}
        set_expr["status"] = status  # 显式 status 恒优先（最后写入覆盖 payload 同名键）
        await self._call(
            lambda: db.update_one(
                COLLECTION,
                {"conversation_id": conversation_id,
                 "client_request_id": client_request_id},
                {"$set": set_expr},
            ),
            reason="IdempotencyRepository.complete 写入失败",
        )

    async def reject(self, conversation_id: str, client_request_id: str, *,
                     error: dict, payload: dict | None = None) -> None:
        """拒绝并持久化 error + 完整 ACK 负载（rejected ACK 前调用）。

        ``$set`` status=STATUS_REJECTED + error + payload（received_at/client_message_id/
        conversation_id_wire 等）——rejected 重放逐字一致（Task 4 的
        RejectedUserAckMessage 需要持久化 receivedAt 原值）。显式 status/error 最后写入
        恒优先（对齐 ``complete()`` 的 status-wins 模式，payload 不得覆盖）。

        Args:
            conversation_id: 目标会话 ID。
            client_request_id: 客户端请求 ID。
            error: rejected ACK 的 error 负载（code/message/retryable/...）。
            payload: 附加 ACK 负载字段（合并进 ``$set``）。
        """
        db = self._require_db()
        set_expr: dict = dict(payload) if payload else {}
        set_expr["status"] = STATUS_REJECTED
        set_expr["error"] = error  # 显式 status/error 恒优先（最后写入覆盖 payload 同名键）
        await self._call(
            lambda: db.update_one(
                COLLECTION,
                {"conversation_id": conversation_id,
                 "client_request_id": client_request_id},
                {"$set": set_expr},
            ),
            reason="IdempotencyRepository.reject 写入失败",
        )

    async def release(self, conversation_id: str, client_request_id: str) -> None:
        """释放 claim（消息被拒/异常后删除，允许重试重新进入）。"""
        db = self._require_db()
        await self._call(
            lambda: db.delete_many(
                COLLECTION,
                {"conversation_id": conversation_id,
                 "client_request_id": client_request_id},
            ),
            reason="IdempotencyRepository.release 删除失败",
        )

    async def restore(self, doc: dict) -> dict:
        """完整记录恢复（BE-P1-06）：缺失插入 / claimed 升级 / 终态防御读回（原子 upsert）。

        区别于 claim()（只写最小 claim 字段），restore() 写入完整 ACK 重放负载
        （received_at/client_message_id/conversation_id_wire/error）——降级期终态补落
        Mongo 后跨重启重放逐字一致。

        过滤器 status∈{claimed,in_progress} + upsert：
        - Mongo 缺失 → 插入完整 doc；
        - Mongo 为 claimed（本进程写穿失败的前进状态）→ $set 升级为完整终态；
        - Mongo 已终态（另一进程 winner / 已落库）→ 过滤器失配，upsert 尝试插入同键 →
          唯一索引 E11000 → non_fatal 防御层读回 winner（不覆盖）。recover() 的
          get-first 已保证终态 doc 不进入本方法（Task 3），E11000 层仅兜底并发/直调。

        Args:
            doc: 完整记录（conversation_id/client_request_id 必含——身份键守卫，缺失
                抛 ValueError；其余为 content_digest/message_id/status/claimed_at/
                expires_at + 终态负载）。

        Returns:
            实际生效的完整记录。本次 insert/claimed 升级成功时返回输入
            doc；并发 E11000 时返回 Mongo 中已提交的 winner，供 recover
            刷新本地镜像。

        Raises:
            ValueError: 缺 conversation_id/client_request_id（编程错误，非存储故障）。
            StorageUnavailableError: 存储不可用。
        """
        db = self._require_db()
        if doc.get("conversation_id") is None or doc.get("client_request_id") is None:
            raise ValueError("IdempotencyRepository.restore 缺少身份键")
        set_expr = {k: v for k, v in doc.items()
                    if k not in ("conversation_id", "client_request_id")}
        try:
            await self._call(
                lambda: db.update_one(
                    COLLECTION,
                    {"conversation_id": doc["conversation_id"],
                     "client_request_id": doc["client_request_id"],
                     "status": {"$in": [STATUS_CLAIMED, "in_progress"]}},
                    {"$set": set_expr},
                    upsert=True,
                ),
                reason="IdempotencyRepository.restore 写入失败",
                non_fatal=_is_duplicate_key,
            )
            return dict(doc)
        except Exception as e:
            if not _is_duplicate_key(e):
                raise
            # 并发 upsert 竞态（罕见 E11000）：读回 winner，不判死 repo（防御层）
            winner = await self._call(
                lambda: db.find(COLLECTION,
                                {"conversation_id": doc["conversation_id"],
                                 "client_request_id": doc["client_request_id"]},
                                limit=1),
                reason="IdempotencyRepository.restore 冲突读回失败",
            )
            if winner:
                return winner[0]
            # 唯一索引冲突但 winner 已被 TTL/并发删除：无法证明恢复完成，fail-closed。
            raise
