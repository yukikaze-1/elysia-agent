"""Token 用量记录类型 — Pydantic BaseModel。"""

from pydantic import BaseModel, Field

from .types import SessionId, IsoTimestamp
from ._utils import now_iso


class TokenUsageEntry(BaseModel):
    """单次 LLM 调用的 token 消耗记录。"""
    session_id: SessionId
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    round_num: int
    timestamp: IsoTimestamp = Field(default_factory=now_iso)
