"""Token 用量记录类型 — Pydantic BaseModel。"""

from pydantic import BaseModel, Field

from .types import SessionId, IsoTimestamp
from ._utils import now_iso


class TokenUsageEntry(BaseModel):
    """单次 LLM 调用的 token 消耗记录。用于持久化到 MongoDB token_usage 集合。"""
    session_id: SessionId
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    round_num: int
    timestamp: IsoTimestamp = Field(default_factory=now_iso)


class TokenUsageRecord(BaseModel):
    """单轮 LLM 调用的 token 消耗快照。用于 AssistantMessage.token_usage.rounds。"""
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    round_num: int
    timestamp: float  # Unix timestamp，来自 TokenTracker.TokenUsage


class TokenUsageSummary(BaseModel):
    """一次 Assistant 回复的 token 用量汇总。嵌入 AssistantMessage.token_usage。"""
    rounds: list[TokenUsageRecord] = Field(default_factory=list)
    turn_total: int = 0
