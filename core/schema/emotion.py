"""EmotionState — EmotionEngine 专用，当前情绪快照。"""

from pydantic import BaseModel, Field

from .types import SessionId, IsoTimestamp
from ._utils import now_iso


class EmotionState(BaseModel):
    """当前情绪状态快照。"""
    session_id: SessionId
    mood: str = "neutral"
    mood_intensity: float = 0.5
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
