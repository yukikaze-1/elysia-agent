"""DesktopCallRepository — Desktop 工具调用（desktop_calls 集合）的 Repository 实现。

批次 4（BE-041/043）：桌面工具「请求 → 投递 → 结果回传」的调用注册表。
Mongo 持久化 + **进程内缓存（读权威）**——幂等去重/状态查询不因 Mongo 离线而失效。

文档 schema（spec §4.1）：
    {
        call_id,              # call_ 前缀，唯一
        tool_name,            # desktop.file.read 等
        tool_version,         # "1.0.0"
        runtime,              # "desktop"
        arguments,            # 原始参数（审计/幂等必需，敏感纪律 D-p）
        arguments_hash,       # sha256(规范化 arguments)，BE-043 篡改防护依据
        reason,               # 请求原因（LLM 生成）
        risk_level,           # safe/medium/high
        permission_required,  # bool
        approval_authority,   # "client"
        source_call_id,       # LLM tool_call_id（D6-07，前端统一领域键）
        request_id,           # 请求追踪 ID（D6-07）
        status,               # pending|accepted|running|succeeded|failed|rejected|cancelled|expired|offline
        target_connection,    # 目标 Electron 连接 ID
        created_at,           # 建档时间（ISO UTC）
        expires_at,           # 超时截止（ISO UTC）
        updated_at,           # 最后更新（ISO UTC，排序/展示用）
        ttl_expires_at,       # 真实 BSON Date（30 天 TTL 基准，B6/P2-01）
        result,               # None | {output, attachments, execution_summary}
        error,                # None | {code, message}
    }

索引：call_id unique + status+updated_at + ttl_expires_at TTL 30 天（对齐 generations）。

进程内缓存语义（review 修正，不沿用 GenerationRepo 离线读返回 None）：
- `_cache[call_id]` 为唯一读权威；写入同步 cache + Mongo（可达时）；
- Mongo 离线（`_db is None` 或写失败）仅写 cache + log——幂等去重/状态查询不失效；
- 启动 `connect()` 成功则全量 reload 入 cache。

状态迁移守卫（spec §4.1 表，`_ALLOWED_TRANSITIONS`）：
- pending → accepted/running（客户端接手）
- pending → succeeded/failed/rejected/cancelled（客户端终态直达——先登记后投递）
- pending → expired（超时未接手）/ offline（断连未接手）
- accepted ↔ running（客户端执行状态往返，Electron 回 tool.status{accepted/running}）
- accepted/running → succeeded/failed/rejected/cancelled（客户端结果）
- accepted/running → expired（超时）/ offline（断连已接手）
- offline → succeeded/failed/rejected/cancelled（晚到结果/重传闭环）
- offline → expired（超时窗口耗尽）
- 终态（succeeded/failed/rejected/cancelled/expired）**不可再迁移**——晚到 status
  仅更新 result/error（记录真实结果供查询），不覆盖终态枚举。

offline 为**准终态**：无主动过期定时器——offline → expired 表项仅为语义完备，
实际靠 ttl_expires_at 的 30d TTL 兜底回收（spec §4.1 显式声明，不要为其加自动过期）。
"""

import logging
from typing import Any

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema.utils import now_iso
from core.storage.base import Repository, bson_expires

logger = logging.getLogger(__name__)

COLLECTION = "desktop_calls"

# TTL：30 天，按真实 BSON Date ttl_expires_at 过期（与 generations 对齐）
DESKTOP_CALLS_TTL_SECONDS = 30 * 86400

# 9 值状态常量
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUS_OFFLINE = "offline"

# 终态集合：不可再迁移（晚到 status 仅记录 result/error，见 mark()）
_TERMINAL = frozenset({
    STATUS_SUCCEEDED, STATUS_FAILED, STATUS_REJECTED, STATUS_CANCELLED, STATUS_EXPIRED,
})

# 未终态、可被断连/超时回收的活跃集合
_ACTIVE_STATES = frozenset({STATUS_PENDING, STATUS_ACCEPTED, STATUS_RUNNING})


def is_active_desktop_call_state(status: object) -> bool:
    """status 是否属于「活跃」态（未终态、可被断连/超时回收）。

    公开谓词（批次 A）：此前跨包消费方直接导入私有 `_ACTIVE_STATES` 集合判断，
    把「哪些状态算活跃」这一领域知识泄漏给了调用方；改为语义化查询后，
    集合本身可自由调整而不影响消费方。

    Args:
        status: 状态字符串（None/未知值一律 False）。

    Returns:
        True 表示处于 pending/accepted/running 之一。
    """
    return status in _ACTIVE_STATES

# 状态迁移守卫表（spec §4.1，权威）。终态为值集合空集（不可再迁移）。
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PENDING: frozenset({
        STATUS_ACCEPTED, STATUS_RUNNING,
        STATUS_SUCCEEDED, STATUS_FAILED, STATUS_REJECTED, STATUS_CANCELLED,
        STATUS_EXPIRED, STATUS_OFFLINE,
    }),
    STATUS_ACCEPTED: frozenset({
        STATUS_RUNNING,  # accepted → running（Electron 回 tool.status{running}）
        STATUS_SUCCEEDED, STATUS_FAILED, STATUS_REJECTED, STATUS_CANCELLED,
        STATUS_EXPIRED, STATUS_OFFLINE,
    }),
    STATUS_RUNNING: frozenset({
        STATUS_ACCEPTED,  # running → accepted（双向放行，fake 已对称）
        STATUS_SUCCEEDED, STATUS_FAILED, STATUS_REJECTED, STATUS_CANCELLED,
        STATUS_EXPIRED, STATUS_OFFLINE,
    }),
    STATUS_OFFLINE: frozenset({
        STATUS_SUCCEEDED, STATUS_FAILED, STATUS_REJECTED, STATUS_CANCELLED, STATUS_EXPIRED,
    }),
    STATUS_SUCCEEDED: frozenset(),
    STATUS_FAILED: frozenset(),
    STATUS_REJECTED: frozenset(),
    STATUS_CANCELLED: frozenset(),
    STATUS_EXPIRED: frozenset(),
}


# 启动全量 reload 的分页块大小（find 默认 limit=100 会截断，显式分批载入）
_RELOAD_CHUNK = 500


class DesktopCallRepository(Repository):
    """Desktop 工具调用注册表 — MongoDB desktop_calls 集合 + 进程内缓存（读权威）。

    缓存为唯一读权威：Mongo 离线时 upsert/mark 仅写 cache + log，
    get/query_pending 从 cache 返回——幂等去重/状态查询不失效。
    """

    def __init__(self, mongodb: DocumentDBBackend | None = None, *,
                 database_name: str = "default",
                 base_url: str | None = None) -> None:
        """初始化桌面调用存储。

        Args:
            mongodb: 可选的文档数据库后端，为 None 时需调用 connect() 建立连接。
            database_name: MongoDB 数据库名称，默认 "default"。
            base_url: MongoDB 连接串，为 None 时从 MONGODB_URI 环境变量读取。
        """
        self._db: DocumentDBBackend | None = mongodb
        self._database_name = database_name
        self._base_url = base_url
        self._injected_db = mongodb is not None
        # 进程内调用缓存（唯一读权威，call_id → 文档）
        self._cache: dict[str, dict] = {}

    # ---- lifecycle ----

    async def connect(self) -> bool:
        """建立 MongoDB 连接，创建索引和 TTL，并全量 reload 入进程内缓存。

        Returns:
            True 表示连接成功，False 表示连接失败（离线模式，仅进程内缓存）。
        """
        if self._db is None:
            try:
                self._db = DocumentDBFactory.create(
                    f"mongodb/{self._database_name}",
                    base_url=self._base_url,
                )
            except Exception as e:
                logger.warning("DesktopCallRepository: MongoDB 不可用 (%s)", e)
                self._db = None
                return False
        try:
            await self._db.ensure_index(COLLECTION, [("call_id", 1)], unique=True)
            await self._db.ensure_index(COLLECTION, [("status", 1), ("updated_at", 1)])
            try:
                # B6/P2-01：TTL 基于真实 BSON Date ttl_expires_at（旧 updated_at ISO 不生效）
                await self._db.ensure_index(
                    COLLECTION, [("ttl_expires_at", 1)],
                    expireAfterSeconds=DESKTOP_CALLS_TTL_SECONDS)
            except Exception as e:
                logger.warning("DesktopCallRepository: TTL 索引创建失败 (%s)", e)
            await self._backfill_ttl()
            await self.reload_from_db()
            logger.info("DesktopCallRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("DesktopCallRepository: MongoDB 不可用 (%s)", e)
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
                        {"call_id": doc.get("call_id")},
                        {"$set": {"ttl_expires_at": bson_expires(30)}})
                except Exception:
                    logger.debug("desktop call ttl 回填失败（幂等，下次重试）", exc_info=True)
        except Exception:
            logger.warning("desktop call ttl 回填查询失败", exc_info=True)

    async def close(self) -> None:
        """关闭数据库连接，释放资源（进程内缓存保留，不阻断后续查询）。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        """健康检查。缓存为读权威，离线功能不失效——真实 ping 反映 Mongo 是否可达（P1-05）。"""
        return await self._health_check()

    # ---- core operations ----

    async def upsert(self, call: dict) -> None:
        """按 call_id upsert 一条调用文档：写 cache + Mongo（可达时）。

        缓存为唯一写权威；Mongo 离线（db=None 或写失败）仅保留 cache + log。

        Args:
            call: 调用文档，须含 call_id。
        """
        call_id = call.get("call_id")
        if not call_id:
            return
        doc = dict(call)
        doc.setdefault("updated_at", now_iso())
        # B6/P2-01：TTL 基准 BSON Date（业务 expires_at 是调用超时，非回收 TTL）
        doc.setdefault("ttl_expires_at", bson_expires(30))
        self._cache[call_id] = doc
        await self._persist(call_id, doc)

    async def get(self, call_id: str) -> dict | None:
        """按 call_id 读调用文档（进程内缓存，离线不失效）。

        Args:
            call_id: 调用 ID。

        Returns:
            文档副本（防止调用方变异污染缓存）；不存在返回 None。
        """
        cached = self._cache.get(call_id)
        return dict(cached) if cached is not None else None

    async def query_pending(self, cutoff: str) -> list[dict]:
        """查询缓存中 pending/accepted/running 且未过期（expires_at > cutoff）的调用。

        Args:
            cutoff: 当前时间 ISO UTC，expires_at 严格大于才算未过期。

        Returns:
            活跃调用文档列表（副本）。
        """
        return [
            dict(doc)
            for doc in self._cache.values()
            if is_active_desktop_call_state(doc.get("status"))
            and (doc.get("expires_at") or "") > cutoff
        ]

    async def mark(self, call_id: str, **fields: Any) -> bool:
        """按 call_id 更新字段，带状态迁移守卫（spec §4.1 表）。

        守卫规则：
        - `fields` 含 status 时校验当前 status → 新 status 合法性；非法返回 False 不写。
        - 终态（succeeded/failed/rejected/cancelled/expired）不可再迁移——晚到 status
          仅更新 result/error（记录真实结果供查询），不覆盖终态枚举；返回 False 表示
          枚举迁移被拒绝。
        - offline → 终态合法（晚到结果/重传闭环）。
        - 同状态重标（幂等）与非 status 字段更新（如补记 result/error）直接放行。

        Args:
            call_id: 调用 ID。
            **fields: 待更新字段（status/result/error 等）。updated_at 缺省自动推进。

        Returns:
            True 表示更新已落地；False 表示状态迁移被拒绝或调用不存在。
        """
        cached = self._cache.get(call_id)
        if cached is None:
            return False
        current = cached.get("status", "")
        new_status = fields.get("status")
        update: dict = dict(fields)
        if "status" in update and update["status"] is None:
            update.pop("status")
        if new_status is not None and new_status != current:
            allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
            if new_status not in allowed:
                if current in _TERMINAL:
                    # 终态晚到 status：不覆盖枚举，仅记录 result/error（真实结果）。
                    late = {k: v for k, v in update.items() if k in ("result", "error")}
                    if not late:
                        return False
                    await self._apply_update(call_id, late)
                    return False
                # 非法迁移：不写，返回 False。
                return False
        await self._apply_update(call_id, update)
        return True

    async def mark_offline_by_connection(self, connection_id: str) -> int:
        """把目标连接的 pending/accepted/running 调用批量标为 offline（断连）。

        只标 `target_connection == connection_id` 的活跃调用；其他连接与终态不动。

        Args:
            connection_id: 断开的目标连接 ID。

        Returns:
            修改的调用数。
        """
        targets = [
            call_id
            for call_id, doc in self._cache.items()
            if doc.get("target_connection") == connection_id
            and is_active_desktop_call_state(doc.get("status"))
        ]
        modified = 0
        for call_id in targets:
            if await self.mark(call_id, status=STATUS_OFFLINE):
                modified += 1
        if modified:
            logger.info(
                "DesktopCallRepository mark_offline_by_connection: %d → offline (conn=%s)",
                modified, connection_id,
            )
        return modified

    async def mark_stale_expired(self, cutoff: str) -> int:
        """把 pending/accepted/running 中 updated_at < cutoff 的调用批量标为 expired。

        启动清理超时孤儿（review 补充）：避免后端重启后遗留 running 态 call 滞留到
        30d TTL。

        Args:
            cutoff: 过期阈值（ISO UTC），updated_at 严格小于才清理。

        Returns:
            修改的调用数。
        """
        targets = [
            call_id
            for call_id, doc in self._cache.items()
            if is_active_desktop_call_state(doc.get("status"))
            and (doc.get("updated_at") or "") < cutoff
        ]
        modified = 0
        for call_id in targets:
            if await self.mark(call_id, status=STATUS_EXPIRED):
                modified += 1
        if modified:
            logger.info("DesktopCallRepository mark_stale_expired: %d → expired", modified)
        return modified

    async def reload_from_db(self) -> None:
        """connect 成功后从 Mongo 全量载入进程内缓存（缓存以 db 快照重建）。

        分页载入（skip/limit 循环）——DocumentDBBackend.find 默认 limit=100 会截断，
        超 100 条调用只载入前 100。db 不可用时静默跳过（保持现有缓存）。
        启动时缓存为空，重建即全量载入。
        """
        if self._db is None:
            return
        loaded: dict[str, dict] = {}
        skip = 0
        try:
            while True:
                docs = await self._db.find(COLLECTION, {}, limit=_RELOAD_CHUNK, skip=skip)
                if not docs:
                    break
                for doc in docs:
                    if doc.get("call_id"):
                        loaded[doc["call_id"]] = dict(doc)
                skip += len(docs)
                if len(docs) < _RELOAD_CHUNK:
                    break
        except Exception as e:
            logger.warning("DesktopCallRepository: 从 Mongo 全量载入失败 (%s)", e)
            return
        self._cache = loaded
        if loaded:
            logger.info("DesktopCallRepository: reload_from_db 载入 %d 条调用", len(loaded))

    # ---- internal helpers ----

    async def _apply_update(self, call_id: str, update: dict) -> None:
        """把字段更新应用到 cache + Mongo（可达时）。updated_at 缺省自动推进。"""
        cached = self._cache.get(call_id)
        if cached is None:
            return
        update = dict(update)
        update.setdefault("updated_at", now_iso())
        cached.update(update)
        await self._persist(call_id, cached)

    async def _persist(self, call_id: str, doc: dict) -> None:
        """异步写 Mongo（dict() 拷贝防 motor 变异，update_one upsert 语义）。"""
        if self._db is None:
            return
        try:
            await self._db.update_one(
                COLLECTION, {"call_id": call_id}, {"$set": dict(doc)}, upsert=True)
        except Exception as e:
            logger.warning(
                "DesktopCallRepository: Mongo 写入失败，仅保留进程内缓存 (%s)", e)
