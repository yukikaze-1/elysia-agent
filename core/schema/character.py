"""CharacterState — CharacterEngine 完整状态，合并 mood + reflector 字段。"""

from typing import TypedDict

from pydantic import BaseModel, Field

from .types import SessionId, IsoTimestamp
from ._utils import now_iso


class StateHistoryEntry(TypedDict):
    timestamp: float
    content: str


class CharacterState(BaseModel):
    """CharacterEngine 完整状态。"""
    session_id: SessionId
    self_l2: str = ""
    state_l3: str = ""
    state_history: list[StateHistoryEntry] = Field(default_factory=list)
    last_awareness_time: float | None = None       # epoch float，非 IsoTimestamp
    compression_last_at: float | None = None         # epoch float，非 IsoTimestamp
    compression_last_count: int = 0
    mood: str = "平静"
    mood_intensity: float = 0.5
    reflector_buffer: list[tuple[str, str]] = Field(default_factory=list)
    reflector_context: list[dict] = Field(default_factory=list)
    self_perception_history: list[dict] = Field(default_factory=list)
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
