"""MemoryReflectorState — MemoryReflector 计数器状态。"""

from pydantic import BaseModel, Field


class MemoryReflectorState(BaseModel):
    mr_round_count: int = 0
    mr_new_memory_count: int = 0
    mr_last_summary: dict[str, object] = Field(default_factory=dict)
