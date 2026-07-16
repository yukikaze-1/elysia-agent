"""core/schema 工具函数 — 类型定义之外的小型辅助函数。

IsoTimestamp: 兼容 ISO 8601 字符串和旧 float Unix 时间戳。
"""

from datetime import datetime, timezone


def _normalize_iso_ts(ts: str | float) -> str:
    """归一化 ISO 8601 时间戳：补时区、校验格式。同时兼容旧 float Unix 时间戳。"""
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat()
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def now_iso() -> str:
    """返回当前 UTC 时间 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _validate_src(v: str) -> str:
    """校验 Part src 字段：不能为空，必须是合法 URL / data URI / 绝对路径 / 存在的本地文件。"""
    from pathlib import Path

    if not v:
        raise ValueError("src 不能为空")
    if v.startswith(("http://", "https://", "data:", "/")):
        return v
    if Path(v).exists():
        return v
    raise ValueError(f"无效的 src: {v!r}")
