"""v2 幂等存储 — clientRequestId → 用户消息记录（进程内 dict + TTL，批次 2）。

语义（spec §4.2）：
- 相同 clientRequestId + 相同内容摘要 → 重发原 ACK，不重复生成；
- 相同 clientRequestId + 不同内容摘要 → rejected + CONTRACT_VALIDATION；
- Demo 单用户，进程内存储够用；重启丢失可接受（不引入 MongoDB 依赖）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class IdempotencyEntry:
    """一次幂等请求的记录。"""

    user_message_id: str      # 预分配用户消息 ID（ACK 与落库一致）
    content_digest: str       # 内容摘要（SHA-256 hex）
    created_at: float         # 创建时间（unix 秒）


class IdempotencyStore:
    """clientRequestId 幂等表，带 TTL 清理。"""

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, IdempotencyEntry] = {}

    def lookup(self, key: str) -> IdempotencyEntry | None:
        """按 clientRequestId 查询；过期条目清除并视为未命中。"""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.created_at > self._ttl:
            self._entries.pop(key, None)
            return None
        return entry

    def store(self, key: str, entry: IdempotencyEntry) -> None:
        """记录一次请求。"""
        self._entries[key] = entry

    def delete(self, key: str) -> None:
        """删除一条幂等记录（批次 7：附件消息队列满 rejected 后删除，允许重试重新进入）。"""
        self._entries.pop(key, None)

    def clear(self) -> None:
        """清空（测试隔离用）。"""
        self._entries.clear()


# 模块级单例（进程内；测试用 reset_store 隔离）
_store = IdempotencyStore()


def get_store() -> IdempotencyStore:
    """返回进程级幂等表单例。"""
    return _store


def reset_store() -> None:
    """清空幂等表（测试隔离）。"""
    _store.clear()
