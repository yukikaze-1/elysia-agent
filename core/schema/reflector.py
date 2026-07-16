"""ReflectorState — CharacterReflector + MemoryReflector 专用。"""

from pydantic import BaseModel, Field

from .types import SessionId, IsoTimestamp
from ._utils import now_iso


class ReflectorState(BaseModel):
    """Reflector buffer/context/计数状态。"""
    session_id: SessionId
    reflector_buffer: list[tuple] = Field(default_factory=list)
    reflector_context: list[dict] = Field(default_factory=list)
    self_perception_history: list[dict] = Field(default_factory=list)
    mr_round_count: int = 0
    mr_new_memory_count: int = 0
    mr_last_summary: dict[str, object] = Field(default_factory=dict)
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
