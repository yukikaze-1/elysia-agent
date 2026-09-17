"""WS 消息契约：generation 域（ARCH-INC-P2-02 物理拆分）。

装配点见 `frontend_contract/ws_message/__init__.py`；该文件是唯一的
`ServerMessage`/`ClientMessage` 联合定义位置。
"""
from __future__ import annotations


from frontend_contract.base import ContractModel
from frontend_contract.rest import ConversationUsage, FinishReason
from pydantic import Field, model_validator
from typing import Any, Literal, Optional
from frontend_contract.ws_message._types import MessageChannel, SegmentKind, TokenUsage, _require_complete_character_identity



class GenerationStartedMessage(ContractModel):
    """生成开始（v2）。messageId = assistant 父消息 ID（generation 开始预分配）。"""

    type: Literal["generation.started"] = "generation.started"
    generationId: str = Field(description="生成实例 ID（gen_ 前缀）")
    messageId: str = Field(description="assistant 父消息 ID")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")
    requestId: Optional[str] = Field(default=None, description="回显 request_id（日志关联），可缺省")
    model: Optional[str] = Field(default=None, description="实际模型，可缺省")
    characterId: Optional[str] = Field(default=None, min_length=1, description="角色 ID（character_profiles=true 后必填）")
    characterInstanceId: Optional[str] = Field(default=None, min_length=1, description="角色实例 ID")
    characterRevision: Optional[int] = Field(default=None, ge=1, description="角色修订号")

    @model_validator(mode="after")
    def _validate_character_identity(self) -> "GenerationStartedMessage":
        _require_complete_character_identity(
            self.characterId, self.characterInstanceId, self.characterRevision)
        return self



class GenerationSegmentStartedMessage(ContractModel):
    """分段开始（v2）。"""

    type: Literal["generation.segment.started"] = "generation.segment.started"
    generationId: str = Field(description="生成实例 ID")
    segmentId: str = Field(description="分段 ID（seg_ 前缀）")
    segmentIndex: int = Field(description="全局分段序号（跨 inner_voice/public_reply 单调递增）")
    channel: MessageChannel = Field(description="分段通道")
    kind: SegmentKind = Field(description="分段类型（P0 仅 text/markdown）")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")



class GenerationDeltaMessage(ContractModel):
    """分段内增量（v2）。"""

    type: Literal["generation.delta"] = "generation.delta"
    generationId: str = Field(description="生成实例 ID")
    segmentId: str = Field(description="分段 ID")
    segmentIndex: int = Field(description="全局分段序号")
    deltaSequence: int = Field(description="段内增量序号（从 0 递增）")
    delta: str = Field(description="文本增量")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")



class GenerationSegmentCompletedMessage(ContractModel):
    """分段完成（v2）——不结束父回复。content 为完整段文本（public_reply 与 inner_voice 段都携带）。"""

    type: Literal["generation.segment.completed"] = "generation.segment.completed"
    generationId: str = Field(description="生成实例 ID")
    segmentId: str = Field(description="分段 ID")
    segmentIndex: int = Field(description="全局分段序号")
    channel: MessageChannel = Field(description="分段通道")
    kind: SegmentKind = Field(description="分段类型")
    content: Optional[str] = Field(default=None, description="完整段文本（客户端无需拼 delta 渲染），可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")



class GenerationCompletedMessage(ContractModel):
    """父完成（v2）——唯一终态（finishReason 只含 stop/tool_calls；取消/错误走 generation.failed）。"""

    type: Literal["generation.completed"] = "generation.completed"
    generationId: str = Field(description="生成实例 ID")
    messageId: str = Field(description="assistant 父消息 ID")
    channel: Literal[MessageChannel.PUBLIC_REPLY] = Field(
        default=MessageChannel.PUBLIC_REPLY, description="父回复通道")
    finishReason: FinishReason = Field(description="完成原因（限 stop/tool_calls）")
    usage: TokenUsage = Field(description="回合 token 统计（turn_total=0/rounds=[] 表示无调用记录）")
    model: str = Field(description="实际模型")
    mood: Optional[str] = Field(default=None, description="心情自由文本（如 `开心`/`平静`），非枚举；前端本地归一，未知值忽略")
    completeness: Literal["complete"] = Field(default="complete", description="完整性（P0 恒 complete）")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")
    conversationUsage: Optional[ConversationUsage] = Field(
        default=None, description="会话级 token 累计（get_session_stats 填充），可缺省")
    characterId: Optional[str] = Field(default=None, min_length=1, description="角色 ID（character_profiles=true 后必填）")
    characterInstanceId: Optional[str] = Field(default=None, min_length=1, description="角色实例 ID")
    characterRevision: Optional[int] = Field(default=None, ge=1, description="角色修订号")

    @model_validator(mode="after")
    def _validate_character_identity(self) -> "GenerationCompletedMessage":
        _require_complete_character_identity(
            self.characterId, self.characterInstanceId, self.characterRevision)
        return self



class GenerationFailedMessage(ContractModel):
    """父失败（v2）——取消/错误统一终态。code 用统一错误码目录。"""

    type: Literal["generation.failed"] = "generation.failed"
    generationId: str = Field(description="生成实例 ID")
    messageId: Optional[str] = Field(default=None, description="assistant 父消息 ID（已接受后恒非空），可缺省")
    model: Optional[str] = Field(default=None, description="实际模型（turn 启动快照，D8），可缺省")
    code: str = Field(description="稳定错误码（GENERATION_PROVIDER_UNAVAILABLE/GENERATION_TIMEOUT/GENERATION_CANCELLED/INTERNAL 等）")
    message: str = Field(description="公开说明")
    retryable: bool = Field(description="是否可重试")
    details: dict[str, Any] = Field(default_factory=dict, description="公开安全详情，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")
    characterId: Optional[str] = Field(default=None, min_length=1, description="角色 ID（character_profiles=true 后必填）")
    characterInstanceId: Optional[str] = Field(default=None, min_length=1, description="角色实例 ID")
    characterRevision: Optional[int] = Field(default=None, ge=1, description="角色修订号")

    @model_validator(mode="after")
    def _validate_character_identity(self) -> "GenerationFailedMessage":
        _require_complete_character_identity(
            self.characterId, self.characterInstanceId, self.characterRevision)
        return self
