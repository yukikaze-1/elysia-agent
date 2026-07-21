"""CompressionTrigger + VersionEntry — L3→L2 压缩版本管理类型。"""
from enum import StrEnum
from typing import TypedDict


class CompressionTrigger(StrEnum):
    COMPRESSION = "compression"
    FLUSH = "flush"
    MANUAL = "manual"


class VersionEntry(TypedDict):
    session_id: str
    version: int
    text: str
    timestamp: str       # IsoTimestamp
    trigger: str          # CompressionTrigger.value
    change_summary: list[str]
