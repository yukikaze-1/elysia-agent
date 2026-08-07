"""Token 用量记录类型 — Pydantic BaseModel。

单一权威定义，各层（内存/持久化/消息嵌入/WS 传输）统一使用。
"""

from typing import Any, cast

from pydantic import BaseModel, Field

from core.schema.types import SessionId, IsoTimestamp
from core.schema.utils import now_iso


class TokenUsage(BaseModel):
    """单次 LLM API 调用的 token 消耗。全系统唯一权威定义。"""
    model: str
    session_id: SessionId
    round_num: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int | None = None        # input 中缓存命中
    reasoning_tokens: int | None = None     # output 中思考 token
    timestamp: IsoTimestamp = Field(default_factory=now_iso)

    @classmethod
    def from_api(cls, usage: dict[str, Any], *, model: str, session_id: SessionId,
                 round_num: int) -> "TokenUsage":
        """从 LLM 层归一化 usage dict 构造（含字段校验）。

        usage 键名契约（由 LLM 层 _extract_usage 产出）：
        {"input_tokens": int, "output_tokens": int, "total_tokens": int,
         "cached_tokens": int | None, "reasoning_tokens": int | None}
        契约语义：cached_tokens/reasoning_tokens 为可选键，provider 未返回时
        _extract_usage 可省略该键；from_api 统一用 .get() 读取所有键
        （必填键缺失 → None → Pydantic ValidationError，契约违约即校验失败，
        而非 KeyError）。仅作校验 + 补 model/session_id/round_num/timestamp。
        """
        return cls(
            model=model,
            session_id=session_id,
            round_num=round_num,
            # cast：契约保证必填键为 int；缺失时传入 None 由 Pydantic 校验失败
            input_tokens=cast(int, usage.get("input_tokens")),
            output_tokens=cast(int, usage.get("output_tokens")),
            total_tokens=cast(int, usage.get("total_tokens")),
            cached_tokens=usage.get("cached_tokens"),
            reasoning_tokens=usage.get("reasoning_tokens"),
        )


class TokenUsageSummary(BaseModel):
    """一次 Assistant 回复的 token 用量汇总。嵌入 AssistantMessage.token_usage。"""
    rounds: list[TokenUsage] = Field(default_factory=list)
    turn_total: int = 0
