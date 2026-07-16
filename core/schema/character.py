"""CharacterState — PersonalityEngine 专用，L2/L3 性格状态 + 压缩追踪。"""

from pydantic import BaseModel, Field

from .types import SessionId, IsoTimestamp
from ._utils import now_iso


class CharacterState(BaseModel):
    """角色性格状态快照。不含情绪和 reflector 域字段。"""
    session_id: SessionId
    self_l2: str = ""
    state_l3: str = ""
    state_history: list[dict] = Field(default_factory=list)
    last_awareness_time: IsoTimestamp | None = None
    compression_last_at: IsoTimestamp | None = None
    compression_last_count: int = 0
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
