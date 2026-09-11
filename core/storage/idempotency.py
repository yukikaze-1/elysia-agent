"""v2 幂等存储 — clientRequestId → 用户消息记录（内存权威 overlay + Mongo 写穿 + 恢复 reconcile）。

语义（spec §1.1/§1.3 + plan Task 2，决策 D5/D5-b/D6）：
- 幂等记录三态：claimed / accepted / rejected（legacy "in_progress"/"completed" 在
  `IdempotencyEntry.from_doc` 防御映射）。
- **内存权威 overlay**：`lookup`/`store` 恒先查内存（TTL 有效即命中）；Mongo 可用时
  store 写穿 claim（acquired 语义来自 repo）；Mongo 报错：产品模式透传（fail-closed，
  调用方映射 503）、dev 模式内存降级（进程内任意 Mongo 切换不去重）。
- `store()` 返回 `StoreResult{entry, acquired}`：acquired=True = 取得所有权（进入业务
  处理）；False = 既有记录（重放/并发 loser/降级去重，恒不进入业务处理）。
- `recover()`（Mongo 恢复 reconcile）：内存权威 overlay 逐条四分支对齐——Mongo 缺失 →
  restore() 补落完整文档（含终态 ACK 负载）；Mongo claimed + 内存终态 → restore() 升级
  为终态；Mongo claimed + 内存 claimed / Mongo 已终态 → 刷新内存为 Mongo 状态（Mongo
  优先）。结束后仅在恢复期间无新补偿事实时同时清
  `_in_memory_fallback`/`_needs_recovery`；单调 recovery epoch 同时覆盖新 key
  降级与已有 key 的 complete/reject 写穿失败。
- `complete()/reject()`：写内存（status + 完整 ACK 负载）+ best-effort repo 写穿
  （失败仅记日志，不抛——条目保留仍可重放）。
- `durable`（D5-b）：`repo is not None and not _in_memory_fallback`；`configure()`
  初值 `_in_memory_fallback = repo is None or not repo.is_connected`，运行时由操作
  结果驱动（Mongo 静默断网经首次失败翻 True）。
- release（消息被拒/异常后删除）与 complete 为 best-effort——失败不阻塞已决定的 ACK。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from core.schema.types import MessageId
from core.storage.base import StorageUnavailableError
from core.storage.idempotency_repo import (
    GRACE_SECONDS,
    STATUS_ACCEPTED,
    STATUS_CLAIMED,
    STATUS_REJECTED,
)

if TYPE_CHECKING:
    from core.storage.idempotency_repo import IdempotencyRepository

logger = logging.getLogger(__name__)

# 默认保留窗口：1 小时（进程内 TTL 与 Mongo TTL 索引同值）
_DEFAULT_TTL_SECONDS = 3600.0

# 新鲜 CLAIMED 命中的 IN_PROGRESS 拒绝 ACK retryAfterMs（= GRACE_SECONDS 毫秒）。
# 语义：原请求大概率仍在途，客户端应在此退避后重试；Task 6 golden 帧同值单源。
# GRACE_SECONDS 单源在 core.storage.idempotency_repo（本文件顶部 import）。
# 本文件 CORE-MOD-P1-01 从 core/input 迁入，与 repo 同包复用。
IN_PROGRESS_RETRY_AFTER_MS = int(GRACE_SECONDS * 1000)


@dataclass
class IdempotencyEntry:
    """一次幂等请求的记录（三态 + 完整 ACK 重放负载）。

    claimed_at/expires_at 用墙钟（time.time()，unix 秒）——跨重启可比、与 Mongo 时间
    字段同基（Global Constraints：勿用 time.monotonic() 与墙钟相减）。
    """

    user_message_id: MessageId   # 预分配用户消息 ID（ACK 与落库一致）
    content_digest: str       # 内容摘要（SHA-256 hex）
    created_at: float         # 创建时间（monotonic，兼容既有构造点；内存 TTL 以 claimed_at 为准）
    status: str = STATUS_CLAIMED  # 三态：claimed / accepted / rejected
    conversation_id: str = ""     # 目标会话（唯一约束维度）
    expires_at: float | None = None  # 内存 TTL 兜底（墙钟 unix 秒；Mongo 侧用 BSON Date）
    claimed_at: float | None = None  # claim 时刻（墙钟 unix 秒；stale/TTL 判定依据）
    received_at: str | None = None   # ACK 重放负载：接收时刻（重放逐字一致）
    client_message_id: str | None = None   # ACK 重放负载：客户端消息 ID
    conversation_id_wire: str | None = None  # ACK 重放负载：wire 会话 ID
    error: dict | None = None  # rejected 的 error 负载（code/message/retryable/...）

    @classmethod
    def from_doc(cls, doc: dict) -> "IdempotencyEntry":
        """从 Mongo 文档构造（lookup/recover 恢复；created_at 无意义——TTL 由 claimed_at 管）。

        防御映射 legacy 状态：in_progress→claimed、completed→accepted；读取完整 ACK
        重放负载字段（received_at/client_message_id/conversation_id_wire/error）。
        """
        raw_status = doc.get("status", STATUS_CLAIMED)
        if raw_status == "in_progress":
            status = STATUS_CLAIMED
        elif raw_status == "completed":
            status = STATUS_ACCEPTED
        else:
            status = raw_status
        return cls(
            user_message_id=MessageId(doc.get("message_id", "")),
            content_digest=doc.get("content_digest", ""),
            created_at=time.monotonic(),
            status=status,
            conversation_id=doc.get("conversation_id", ""),
            expires_at=_doc_expires(doc.get("expires_at")),
            claimed_at=doc.get("claimed_at"),
            received_at=doc.get("received_at"),
            client_message_id=doc.get("client_message_id"),
            conversation_id_wire=doc.get("conversation_id_wire"),
            error=doc.get("error"),
        )


@dataclass
class StoreResult:
    """store 层 claim 所有权结果（D5；与 repo 层 ClaimResult 语义一致、分层独立）。

    entry: 实际生效的记录（新 claim / 并发 winner / dev 降级既有条目）。
    acquired: True = 本次取得所有权（调用方进入业务处理）；False = 既有记录
              （重放/并发 loser/降级去重，恒不进入业务处理）。
    """

    entry: IdempotencyEntry
    acquired: bool


class IdempotencyStore:
    """clientRequestId 幂等表门面：内存权威 overlay + Mongo 写穿（durable）或纯内存。

    后端选择：
    - ``configure(repo, product_mode)`` 后 repo 非 None → 优先 Mongo（durable）；
      初值由 ``repo.is_connected`` 决定（D5-b，修复「repo 对象非空即 durable」误报）。
    - Mongo 操作抛 StorageUnavailableError：产品模式 → 透传（调用方 503）；dev 模式 →
      本次落内存降级，``durable=False``（Mongo 恢复后经 ``recover()`` 回 durable）。
    - repo 为 None（未配置）→ 纯内存（测试/无 Mongo 场景），durable=False。
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[tuple[str, str], IdempotencyEntry] = {}
        self._repo: IdempotencyRepository | None = None
        self._product_mode = False
        self._in_memory_fallback = True
        self._needs_recovery = False   # BE-P1-07：存在未 reconcile 到 Mongo 的内存记录
        self._recovery_epoch = 0       # 单调补偿事实版本，防 recover await 间隙误清位

    # ---- 配置 ----

    def configure(self, repo: "IdempotencyRepository | None", *,
                  product_mode: bool) -> None:
        """配置持久化后端与产品/dev 模式（create_app 调用，不触碰内存表）。

        durable 初值（D5-b）：repo 为 None 或未连接（``is_connected=False``）→ 内存
        降级窗口。运行时由操作结果驱动（既有 ``_in_memory_fallback`` 机制，Mongo
        静默断网经首次失败翻 True）。
        """
        self._repo = repo
        self._product_mode = product_mode
        self._in_memory_fallback = repo is None or not repo.is_connected

    @property
    def durable(self) -> bool:
        """当前是否完整持久化（BE-P1-07 强化）：repo 已配置 且 非操作级降级
        且 无未 reconcile 记录。server_info 能力暴露用——无关成功操作不再能静默清零。
        """
        return (self._repo is not None and not self._in_memory_fallback
                and not self._needs_recovery)

    @property
    def configured(self) -> bool:
        """是否配置了持久化后端（repo 非 None；恢复触发 guard，防 repo-None 死循环）。"""
        return self._repo is not None

    @property
    def needs_recovery(self) -> bool:
        """是否有未 reconcile 到 Mongo 的内存记录（/ready 门禁信号）。"""
        return self._repo is not None and self._needs_recovery

    # ---- operations ----

    async def lookup(self, conversation_id: str, client_request_id: str
                     ) -> IdempotencyEntry | None:
        """按 (conversation_id, client_request_id) 查询；内存优先（TTL 有效即命中）。

        内存权威 overlay（D5）：恒查内存（命中即返回）→ miss 才查 Mongo（跨重启镜像）；
        Mongo 报错：产品模式透传（fail-closed）/ dev 视为 miss。过期条目视为未命中。
        """
        mem = self._memory_lookup(conversation_id, client_request_id)
        if mem is not None:
            return mem
        if self._repo is not None:
            try:
                doc = await self._repo.get(conversation_id, client_request_id)
            except StorageUnavailableError:
                if self._product_mode:
                    raise  # fail-closed：调用方映射 503
                self._in_memory_fallback = True
                return None
            self._in_memory_fallback = False
            if doc is not None:
                entry = IdempotencyEntry.from_doc(doc)
                self._entries[(conversation_id, client_request_id)] = entry
                return entry
        return None

    async def store(self, conversation_id: str, client_request_id: str,
                    entry: IdempotencyEntry) -> StoreResult:
        """记录 claim，返回 ``StoreResult{entry, acquired}``。

        内存权威 overlay（D5）：
        - repo 非空 → 恒 ``repo.claim``：acquired=True 写内存返回 ``(entry, True)``；
          acquired=False 把 winner 也写内存返回 ``(winner, False)``（内存镜像完整）；
        - **内存降级窗口（``_in_memory_fallback``）→ 内存优先短路**：同键已有有效内存
          entry 直接返回 ``(mem, False)``，不触碰 Mongo——关闭「down→up 且 recover()
          未跑时 repo.claim 对内存 claim 重新 insert 导致重复处理」窗口（D5 进程内去重）；
        - repo 报错 → 产品模式透传（fail-closed）；dev 模式内存降级（内存已有 →
          ``(mem, False)``，否则插入 → ``(entry, True)``）；
        - repo 为 None（纯内存）→ 保留内存优先去重：同键二次返回既有 ``(entry, False)``，
          不生成新 message_id（防 dev/无 Mongo 双生成）。
        """
        if self._repo is not None:
            # 降级/未 reconcile 窗口：同键有效内存 entry 恒去重短路（不触碰 Mongo）——
            # 关闭「down→up 且 recover() 未跑时 repo.claim 对内存 claim 重新 insert 导致
            # 重复处理」窗口（BE-P1-07）。正常路径（已 reconcile）不走短路，保留
            # repo.claim 的 stale 接管（TAKEOVER）语义。
            if self._in_memory_fallback or self._needs_recovery:
                prev = self._memory_lookup(conversation_id, client_request_id)
                if prev is not None:
                    return StoreResult(entry=prev, acquired=False)
            try:
                result = await self._repo.claim(
                    conversation_id, client_request_id,
                    content_digest=entry.content_digest,
                    message_id=entry.user_message_id,
                    status=entry.status,
                    claimed_at=entry.claimed_at if entry.claimed_at is not None else time.time(),
                    expires_at=_mongo_expiry(self._ttl),
                )
            except StorageUnavailableError:
                if self._product_mode:
                    raise  # fail-closed：调用方映射 503
                self._in_memory_fallback = True
                # dev 内存降级：内存优先去重
                prev = self._memory_lookup(conversation_id, client_request_id)
                if prev is not None:
                    return StoreResult(entry=prev, acquired=False)
                self._apply_timestamps(entry)
                self._entries[(conversation_id, client_request_id)] = entry
                self._mark_needs_recovery()  # BE-P1-07：降级写入未 reconcile
                return StoreResult(entry=entry, acquired=True)
            self._in_memory_fallback = False
            # acquired=True（新插入/接管）与 False（并发 loser/重放）都写内存
            winner = IdempotencyEntry.from_doc(result.entry)
            self._entries[(conversation_id, client_request_id)] = winner
            return StoreResult(entry=winner, acquired=result.acquired)
        # 纯内存路径（repo=None）：保留内存优先去重（同键二次返回既有 entry）
        prev = self._memory_lookup(conversation_id, client_request_id)
        if prev is not None:
            return StoreResult(entry=prev, acquired=False)
        self._apply_timestamps(entry)
        self._entries[(conversation_id, client_request_id)] = entry
        return StoreResult(entry=entry, acquired=True)

    async def recover(self) -> None:
        """Mongo 恢复 reconcile（D5 + BE-P1-06/07）：对每条内存 entry 与 Mongo 对齐。

        内存权威 overlay 逐条对齐，四分支：
        - Mongo 缺失 → restore() 插入完整文档（含终态 ACK 负载）；
        - Mongo claimed + 内存终态（本进程写穿失败的前进状态）→ restore() 升级为终态；
        - Mongo claimed + 内存 claimed → 刷新内存为 Mongo 状态（Mongo 的 message_id
          权威，防多 worker 翻转）；
        - Mongo 已终态 → 刷新内存为 Mongo 状态（Mongo 优先、读时一致）。
        跨重启重放逐字一致（修复 S1：不再用陈旧 claimed 覆盖前进终态）。结束后
        ``_in_memory_fallback = False`` 与 ``_needs_recovery = False`` 只在恢复期间
        ``_recovery_epoch`` 未变时同步清除。任意并发降级新 key，或快照内已有 key
        的 complete/reject 写穿失败，都会递增 epoch，强制保留补偿位到下一轮。
        任一 get/restore 失败 → 异常冒泡，不翻 durable（下轮健康循环重试）。
        repo 为 None（纯内存）→ no-op。
        """
        if self._repo is None:
            return  # 纯内存路径无可 reconcile
        recovery_epoch = self._recovery_epoch
        for (conversation_id, client_request_id), entry in list(self._entries.items()):
            if self._is_stale(entry):
                # 过期内存记录（Mongo 已 TTL 清除）→ 不复活
                self._entries.pop((conversation_id, client_request_id), None)
                continue
            doc = await self._repo.get(conversation_id, client_request_id)
            mongo_terminal = doc is not None and _is_terminal_status(doc.get("status"))
            memory_terminal = entry.status in (STATUS_ACCEPTED, STATUS_REJECTED)
            if doc is not None and (mongo_terminal or not memory_terminal):
                # Mongo 已终态（另一进程 winner / 已落库）或双方皆 claimed（Mongo 的
                # message_id 权威）→ 刷新内存为 Mongo 状态
                self._entries[(conversation_id, client_request_id)] = IdempotencyEntry.from_doc(doc)
                continue
            # Mongo 缺失，或（Mongo claimed + 内存终态 = 本进程写穿失败的前进状态）→
            # restore 原子 upsert 完整文档（缺失→插入 / claimed→升级终态，含重放负载）
            restored = await self._repo.restore(
                _build_full_doc(conversation_id, client_request_id, entry, self._ttl))
            # get-first 后仍可输掉 E11000 竞态；用 restore 读回的 Mongo winner
            # 刷新本地，不保留 loser 并误报 durable。
            self._entries[(conversation_id, client_request_id)] = IdempotencyEntry.from_doc(restored)
        if self._recovery_epoch == recovery_epoch:
            # 恢复期间无新补偿事实：本轮已 reconcile 全部，同时清两位。
            self._in_memory_fallback = False
            self._needs_recovery = False

    async def delete(self, conversation_id: str, client_request_id: str) -> None:
        """释放 claim（消息被拒/异常后删除，允许重试重新进入）。

        best-effort：失败仅记日志（条目保留到 TTL——重试会重放原 ACK，不重复
        generation，安全方向）；绝不抛（错误处理路径不应被 cleanup 打断）。
        """
        if self._repo is not None:
            try:
                await self._repo.release(conversation_id, client_request_id)
                self._in_memory_fallback = False
            except StorageUnavailableError:
                # 两种模式都吞：release 是 cleanup，失败不改变已决定的拒绝语义；
                # 但 repo 不可用 → 非 durable 窗口（capabilities 如实暴露）
                self._in_memory_fallback = True
        self._entries.pop((conversation_id, client_request_id), None)

    async def complete(self, conversation_id: str, client_request_id: str, *,
                       received_at: str, client_message_id: str | None,
                       conversation_id_wire: str | None) -> None:
        """标记 claim 已完成（accepted ACK 前调用），持久化完整 ACK 重放负载。

        写内存（status=accepted + received_at/client_message_id/conversation_id_wire）
        + best-effort repo 写穿（失败仅记日志，不抛——条目保留仍可重放）。
        """
        key = (conversation_id, client_request_id)
        entry = self._entries.get(key)
        if entry is not None:
            entry.status = STATUS_ACCEPTED
            entry.received_at = received_at
            entry.client_message_id = client_message_id
            entry.conversation_id_wire = conversation_id_wire
        if self._repo is not None:
            try:
                await self._repo.complete(
                    conversation_id, client_request_id,
                    payload={"received_at": received_at,
                             "client_message_id": client_message_id,
                             "conversation_id_wire": conversation_id_wire},
                )
                self._in_memory_fallback = False
            except StorageUnavailableError:
                # 消息已 ACK：仅丢状态字段，不抛；内存终态超前 Mongo → 需 recover 补落
                logger.warning("IdempotencyStore.complete 写穿失败（key=%s/%s）",
                               conversation_id, client_request_id)
                self._in_memory_fallback = True
                if entry is not None:
                    # 仅内存有 entry（可 reconcile）才置位——无 entry 的写穿失败
                    # 无记录可补落，置位会成假 flag（recover 空转清位）
                    self._mark_needs_recovery()  # BE-P1-06/07：终态未落 Mongo

    async def reject(self, conversation_id: str, client_request_id: str, *,
                     code: str, message: str, retryable: bool,
                     received_at: str, client_message_id: str | None,
                     conversation_id_wire: str | None,
                     details: dict | None = None,
                     retry_after_ms: int | None = None) -> None:
        """拒绝并持久化 error 负载（rejected ACK 前调用）。

        写内存（status=rejected + 完整负载 + error）+ best-effort repo 写穿（失败仅记
        日志，不抛）——error 负载含 code/message/retryable + 可选 details/retryAfterMs，
        完整 ACK 负载（received_at/client_message_id/conversation_id_wire）经
        ``repo.reject(payload=...)`` 一并落库（重放逐字一致，spec §1.1）。
        """
        error: dict = {"code": code, "message": message, "retryable": retryable}
        if details is not None:
            error["details"] = details
        if retry_after_ms is not None:
            error["retryAfterMs"] = retry_after_ms
        key = (conversation_id, client_request_id)
        entry = self._entries.get(key)
        if entry is not None:
            entry.status = STATUS_REJECTED
            entry.error = error
            entry.received_at = received_at
            entry.client_message_id = client_message_id
            entry.conversation_id_wire = conversation_id_wire
        if self._repo is not None:
            try:
                await self._repo.reject(
                    conversation_id, client_request_id,
                    error=error,
                    payload={"received_at": received_at,
                             "client_message_id": client_message_id,
                             "conversation_id_wire": conversation_id_wire},
                )
                self._in_memory_fallback = False
            except StorageUnavailableError:
                logger.warning("IdempotencyStore.reject 写穿失败（key=%s/%s）",
                               conversation_id, client_request_id)
                self._in_memory_fallback = True
                if entry is not None:
                    self._mark_needs_recovery()  # BE-P1-06/07：终态未落 Mongo

    # ---- 内部 ----

    def _mark_needs_recovery(self) -> None:
        """记录新的「内存超前 Mongo」事实；重复置位也必须递增版本。"""
        self._needs_recovery = True
        self._recovery_epoch += 1

    def _memory_lookup(self, conversation_id: str, client_request_id: str
                       ) -> IdempotencyEntry | None:
        key = (conversation_id, client_request_id)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._is_stale(entry):
            self._entries.pop(key, None)
            return None
        return entry

    def _is_stale(self, entry: IdempotencyEntry) -> bool:
        """内存 TTL 判定：墙钟（time.time()）基于 claimed_at（Global Constraints）。

        勿用 time.monotonic()——单调钟与墙钟相减为巨大负数，TTL 判定永不触发。
        """
        claimed_at = entry.claimed_at
        if claimed_at is None:
            return False  # legacy/未知：视为有效（随 Mongo TTL 或下次 store 覆盖）
        return time.time() - claimed_at > self._ttl

    def _apply_timestamps(self, entry: IdempotencyEntry) -> None:
        """内存条目补墙钟时间戳（claimed_at 缺省取当前；expires_at 缺省 = now + TTL）。"""
        now = time.time()
        if entry.claimed_at is None:
            entry.claimed_at = now
        if entry.expires_at is None:
            entry.expires_at = now + self._ttl

    def reset(self) -> None:
        """清空内存表并恢复未配置态（测试隔离）。"""
        self._entries.clear()
        self._repo = None
        self._product_mode = False
        self._in_memory_fallback = True
        self._needs_recovery = False   # BE-P1-07：测试隔离清位（configure 不清，防 S2 复开）
        self._recovery_epoch = 0


def _mongo_expiry(ttl_seconds: float) -> datetime:
    """Mongo expires_at 字段：aware UTC datetime（BSON Date，TTL 索引到期删除）。"""
    return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)


def _doc_expires(value: object) -> float | None:
    """Mongo 文档 expires_at（BSON Date）→ 内存墙钟 unix 秒；缺失/未知 → None。"""
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _is_terminal_status(status: str | None) -> bool:
    """Mongo status 是否终态（accepted/rejected + legacy completed）。"""
    return status in (STATUS_ACCEPTED, STATUS_REJECTED, "completed")


def _build_full_doc(conversation_id: str, client_request_id: str,
                    entry: IdempotencyEntry, ttl_seconds: float) -> dict:
    """从内存 entry 构建 Mongo 完整文档（claim 字段 + 终态 ACK 负载）。

    client_request_id 是 _entries 的 dict key 部分、不在 entry 内，由调用方传入；
    ttl_seconds 传 store 的 self._ttl（与 claim() 写穿同基，防自定义 TTL 分裂）。
    """
    full = {
        "conversation_id": conversation_id,
        "client_request_id": client_request_id,
        "content_digest": entry.content_digest,
        "message_id": entry.user_message_id,
        "status": entry.status,
        "claimed_at": entry.claimed_at if entry.claimed_at is not None else time.time(),
        "expires_at": _mongo_expiry(ttl_seconds),
    }
    if entry.received_at is not None:
        full["received_at"] = entry.received_at
    if entry.client_message_id is not None:
        full["client_message_id"] = entry.client_message_id
    if entry.conversation_id_wire is not None:
        full["conversation_id_wire"] = entry.conversation_id_wire
    if entry.error is not None:
        full["error"] = entry.error
    return full


# 模块级单例（进程内；测试用 reset_store 隔离）
_store = IdempotencyStore()


def get_store() -> IdempotencyStore:
    """返回进程级幂等表门面。"""
    return _store


def configure_store(repo: "IdempotencyRepository | None", *, product_mode: bool) -> None:
    """配置幂等存储后端（create_app 调用；repo=None → 纯内存）。"""
    _store.configure(repo, product_mode=product_mode)


def reset_store() -> None:
    """清空幂等表并恢复未配置态（测试隔离）。"""
    _store.reset()
