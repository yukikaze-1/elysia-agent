"""core/schema 工具函数 — 类型定义之外的小型辅助函数（共享工具模块）。

now_iso:          跨包共享（core/personality、cognitive、input、storage、memory 等）。
_normalize_iso_ts / _validate_src: schema 内部私有 helper（下划线前缀，勿跨包引用）。
"""

from datetime import datetime, timezone


def _normalize_iso_ts(ts: str | float) -> str:
    """归一化 ISO 8601 时间戳：补时区、校验格式。同时兼容旧 float Unix 时间戳。

    Args:
        ts: ISO 8601 字符串或 Unix 时间戳（float/int）。

    Returns:
        归一化后的 ISO 8601 UTC 时间戳字符串。
    """
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat()
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def now_iso() -> str:
    """返回当前 UTC 时间 ISO 8601 字符串。

    Returns:
        当前 UTC 时间的 ISO 8601 格式字符串。
    """
    return datetime.now(timezone.utc).isoformat()


def _validate_src(v: str) -> str:
    """校验 Part src 字段：不能为空，必须是合法 URL / data URI / 绝对路径 / 存在的本地文件。

    Args:
        v: 待校验的 src 字符串。

    Returns:
        校验通过的原字符串。

    Raises:
        ValueError: src 为空或无法解析为有效路径/URL。
    """
    from pathlib import Path

    if not v:
        raise ValueError("src 不能为空")
    if v.startswith(("http://", "https://", "data:", "/")):
        return v
    if Path(v).exists():
        return v
    raise ValueError(f"无效的 src: {v!r}")
