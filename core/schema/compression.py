"""CompressionTrigger + VersionEntry — L3→L2 压缩版本管理类型。"""
from enum import StrEnum
from typing import TypedDict


class CompressionTrigger(StrEnum):
    COMPRESSION = "compression"
    FLUSH = "flush"
    MANUAL = "manual"
    C6_REFRESH = "c6_refresh"       # L2 更新后 C6 自动刷新


class VersionEntry(TypedDict):
    session_id: str
    version: int
    text: str
    timestamp: str       # IsoTimestamp
    trigger: str          # CompressionTrigger.value
    change_summary: list[str]
    content_type: str     # "l2" | "c6" — 版本所属的 content 类型
