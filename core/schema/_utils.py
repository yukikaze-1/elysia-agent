"""core/schema 工具函数 — 类型定义之外的小型辅助函数。"""

from datetime import datetime, timezone


def _normalize_iso_ts(ts: str) -> str:
    """归一化 ISO 8601 时间戳：补时区、校验格式。"""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def now_iso() -> str:
    """返回当前 UTC 时间 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()
