"""v2 幂等存储 — clientRequestId → 用户消息记录（进程内 dict + TTL，批次 2；D6c 持久化）。

语义（spec §4.2 + plan Task D6c）：
- 相同 clientRequestId + 相同内容摘要 → 重发原 ACK（同 messageId），不重复生成；
- 相同 clientRequestId + 不同内容摘要 → rejected + CONTRACT_VALIDATION；
- D6c：claim 持久化到 Mongo（conversation_id + client_request_id 唯一约束 +
  expires_at BSON Date TTL）——ACK 丢失 + 后端重启后重试不重复生成。
- **产品/dev 模式**（决策 2026-08-11）：非回环（产品）绑定 + Mongo 离线 →
  lookup/store fail-closed raise（调用方映射 503 STORAGE_UNAVAILABLE）；回环
  dev + Mongo 离线 → 内存降级，`durable=False` 经 server_info.features.
  idempotency_durable 暴露非 durable 窗口。
- release（消息被拒/异常后删除）与 complete（标记已完成）为 best-effort——
  失败不阻塞已决定的 ACK（条目保留仍可重放，仅丢状态更新/允许重试）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from core.schema.types import MessageId
from core.storage.base import StorageUnavailableError

if TYPE_CHECKING:
    from core.storage.idempotency_repo import IdempotencyRepository

# 默认保留窗口：1 小时（进程内 TTL 与 Mongo TTL 索引同值）
_DEFAULT_TTL_SECONDS = 3600.0


@dataclass
class IdempotencyEntry:
    """一次幂等请求的记录。"""

    user_message_id: MessageId   # 预分配用户消息 ID（ACK 与落库一致）
    content_digest: str       # 内容摘要（SHA-256 hex）
    created_at: float         # 创建时间（unix 秒；内存 TTL 依据）
    status: str = "in_progress"   # in_progress / completed（D6c）
    conversation_id: str = ""     # 目标会话（D6c，唯一约束维度）
    expires_at: float | None = None  # 内存 TTL 兜底（Mongo 侧用 BSON Date）

    @classmethod
    def from_doc(cls, doc: dict) -> "IdempotencyEntry":
        """从 Mongo 文档构造（lookup 恢复；created_at 无意义——TTL 由 Mongo 管）。"""
        return cls(
            user_message_id=MessageId(doc.get("message_id", "")),
            content_digest=doc.get("content_digest", ""),
            created_at=time.monotonic(),
            status=doc.get("status", "in_progress"),
            conversation_id=doc.get("conversation_id", ""),
        )


class IdempotencyStore:
    """clientRequestId 幂等表门面：Mongo 持久化（durable）或进程内 dict（降级）。

    后端选择：
    - ``configure(repo, product_mode)`` 后 repo 非 None → 优先 Mongo（durable）。
    - Mongo 操作抛 StorageUnavailableError：产品模式 → 透传（调用方 503）；
      dev 模式 → 本次落内存降级，``durable=False``（Mongo 恢复后自动回 durable）。
    - repo 为 None（未配置）→ 纯内存（测试/无 Mongo 场景），durable=False。
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[tuple[str, str], IdempotencyEntry] = {}
        self._repo: IdempotencyRepository | None = None
        self._product_mode = False
        self._in_memory_fallback = True

    # ---- 配置 ----

    def configure(self, repo: "IdempotencyRepository | None", *,
                  product_mode: bool) -> None:
        """配置持久化后端与产品/dev 模式（create_app 调用，不触碰内存表）。"""
        self._repo = repo
        self._product_mode = product_mode
        self._in_memory_fallback = repo is None

    @property
    def durable(self) -> bool:
        """当前是否持久化（Mongo 可用且非降级）。server_info 能力暴露用。"""
        return self._repo is not None and not self._in_memory_fallback

    # ---- operations ----

    async def lookup(self, conversation_id: str, client_request_id: str
                     ) -> IdempotencyEntry | None:
        """按 (conversation_id, client_request_id) 查询；过期条目视为未命中。"""
        if self._repo is not None:
            try:
                doc = await self._repo.get(conversation_id, client_request_id)
            except StorageUnavailableError:
                if self._product_mode:
                    raise  # fail-closed：调用方映射 503
                self._in_memory_fallback = True
            else:
                self._in_memory_fallback = False
                if doc is not None:
                    return IdempotencyEntry.from_doc(doc)
                return None
        return self._memory_lookup(conversation_id, client_request_id)

    async def store(self, conversation_id: str, client_request_id: str,
                    entry: IdempotencyEntry) -> IdempotencyEntry:
        """记录 claim。返回实际生效的 entry（并发同键时可能是既有记录）。

        并发同键双写：Mongo 唯一索引保证单记录，后写者经 ``claim`` 返回既有记录，
        调用方用返回的 message_id 重放 ACK（不重复 generation）。
        """
        if self._repo is not None:
            try:
                existing = await self._repo.claim(
                    conversation_id, client_request_id,
                    content_digest=entry.content_digest,
                    message_id=entry.user_message_id,
                    status=entry.status,
                    expires_at=_mongo_expiry(self._ttl),
                )
            except StorageUnavailableError:
                if self._product_mode:
                    raise  # fail-closed：调用方映射 503
                self._in_memory_fallback = True
            else:
                self._in_memory_fallback = False
                if existing is not None:
                    return IdempotencyEntry.from_doc(existing)
                return entry
        # 内存路径（无 repo / dev 降级）
        key = (conversation_id, client_request_id)
        prev = self._memory_lookup(conversation_id, client_request_id)
        if prev is not None:
            return prev
        self._entries[key] = entry
        return entry

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

    async def complete(self, conversation_id: str, client_request_id: str) -> None:
        """标记 claim 已完成（accepted ACK 后调用）。

        best-effort：失败仅丢状态更新（条目保留，重试仍重放原 ACK），不抛。
        """
        if self._repo is not None:
            try:
                await self._repo.complete(conversation_id, client_request_id)
                self._in_memory_fallback = False
            except StorageUnavailableError:
                # 消息已 ACK：仅丢状态字段，不抛；repo 不可用 → 非 durable 窗口
                self._in_memory_fallback = True
        entry = self._entries.get((conversation_id, client_request_id))
        if entry is not None:
            entry.status = "completed"

    # ---- 内部 ----

    def _memory_lookup(self, conversation_id: str, client_request_id: str
                       ) -> IdempotencyEntry | None:
        key = (conversation_id, client_request_id)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.created_at > self._ttl:
            self._entries.pop(key, None)
            return None
        return entry

    def reset(self) -> None:
        """清空内存表并恢复未配置态（测试隔离）。"""
        self._entries.clear()
        self._repo = None
        self._product_mode = False
        self._in_memory_fallback = True


def _mongo_expiry(ttl_seconds: float) -> datetime:
    """Mongo expires_at 字段：aware UTC datetime（BSON Date，TTL 索引到期删除）。"""
    return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)


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
