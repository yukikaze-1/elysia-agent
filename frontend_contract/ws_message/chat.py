"""WS 消息契约：chat 域（ARCH-INC-P2-02 物理拆分）。

装配点见 `frontend_contract/ws_message/__init__.py`；该文件是唯一的
`ServerMessage`/`ClientMessage` 联合定义位置。
"""
from __future__ import annotations


from frontend_contract.base import ContractModel, TsNull
from pydantic import ConfigDict, Discriminator, Field, model_validator
from typing import Annotated, Any, Literal, Optional, Union
from frontend_contract.ws_message._types import AttachmentRef, AudioFormat, Live2DDirectiveState, ServiceName, TokenStats, TokenTurnStats, _require_complete_character_identity



class DeltaMessage(ContractModel):
    """流式文字增量。"""

    type: Literal["delta"] = "delta"
    content: str = Field(description="文本内容")
    stream_id: Optional[int] = Field(default=None, description="TTS 流 ID，可缺省")



class MessageMessage(ContractModel):
    """完整消息推送（非流式最终文本）。"""

    type: Literal["message"] = "message"
    role: Optional[str] = Field(default=None, description="消息角色，可缺省")
    content: Optional[str] = Field(default=None, description="消息文本内容，可缺省")
    mood: Optional[str] = Field(default=None, description="情绪标签，可缺省")



class TokenUsageMessage(ContractModel):
    """token 消耗统计推送。"""

    type: Literal["token_usage"] = "token_usage"
    turn: Optional[TokenTurnStats] = Field(default=None, description="本轮统计，可缺省")
    session: Optional[TokenStats] = Field(default=None, description="会话统计，可缺省")



class ExpressionMessage(ContractModel):
    """表情/情绪变更；角色字段迁移期可缺省，出现时必须为完整三元组。"""

    type: Literal["expression"] = "expression"
    expression: str = Field(description="表情/情绪标识")
    intensity: Optional[float] = Field(default=None, description="强度 0.0~1.0，可缺省")
    conversationId: Optional[str] = Field(default=None, min_length=1, description="会话 ID，可缺省")
    generationId: Optional[str] = Field(default=None, min_length=1, description="生成 ID；非生成表现可缺省")
    characterId: Optional[str] = Field(default=None, min_length=1, description="角色 ID，可缺省")
    characterInstanceId: Optional[str] = Field(default=None, min_length=1, description="角色实例 ID，可缺省")
    characterRevision: Optional[int] = Field(default=None, ge=1, description="角色修订号，可缺省")

    @model_validator(mode="after")
    def _validate_character_identity(self) -> "ExpressionMessage":
        _require_complete_character_identity(
            self.characterId, self.characterInstanceId, self.characterRevision)
        return self



class Live2DDirectiveMessage(ContractModel):
    """后端 Live2D 表现建议；客户端按现实态优先级仲裁。"""

    type: Literal["live2d.directive"] = "live2d.directive"
    conversationId: Optional[str] = Field(default=None, min_length=1, description="会话 ID，可缺省")
    generationId: Optional[str] = Field(default=None, min_length=1, description="生成 ID；非生成指令可缺省")
    characterId: Optional[str] = Field(default=None, min_length=1, description="角色 ID，可缺省")
    characterInstanceId: Optional[str] = Field(default=None, min_length=1, description="角色实例 ID，可缺省")
    characterRevision: Optional[int] = Field(default=None, ge=1, description="角色修订号，可缺省")
    expression: Optional[str] = Field(default=None, min_length=1, description="表情资源标识，可缺省")
    motion: Optional[str] = Field(default=None, min_length=1, description="动作资源标识，可缺省")
    state: Optional[Live2DDirectiveState] = Field(default=None, description="低优先级表现状态，可缺省")
    priority: Optional[int] = Field(default=None, ge=0, le=3, description="建议优先级 0~3，可缺省")
    durationMs: Optional[int] = Field(default=None, ge=0, le=30_000, description="持续时间毫秒，可缺省")

    @model_validator(mode="after")
    def _validate_character_identity(self) -> "Live2DDirectiveMessage":
        _require_complete_character_identity(
            self.characterId, self.characterInstanceId, self.characterRevision)
        return self



class StateMessage(ContractModel):
    """FSM 状态变更。"""

    type: Literal["state"] = "state"
    state: str = Field(description="FSM 状态名")



class AudioMessage(ContractModel):
    """音频流 chunk（base64 编码）。"""

    type: Literal["audio"] = "audio"
    stream_id: int = Field(description="音频流 ID")
    seq: int = Field(description="chunk 序号（从 0 递增）")
    data: str = Field(description="base64 音频数据")
    is_final: bool = Field(default=False, description="是否最终块")



class AudioEndMessage(ContractModel):
    """音频流结束标记。"""

    type: Literal["audio_end"] = "audio_end"
    stream_id: int = Field(description="音频流 ID")
    format: Optional[AudioFormat] = Field(default=AudioFormat.WAV, description="音频格式，可缺省")



class TypingMessage(ContractModel):
    """输入状态变更。"""

    type: Literal["typing"] = "typing"
    typing: bool = Field(description="True 开始输入，False 结束")



class TranscriptionMessage(ContractModel):
    """STT 转写回显。"""

    type: Literal["transcription"] = "transcription"
    text: str = Field(description="转写文本")
    emotion: Optional[str] = Field(default=None, description="情绪标签，可缺省")
    events: Optional[list[str]] = Field(default=None, description="语音事件列表，可缺省")



class ActionMessage(ContractModel):
    """角色动作事件（工具执行中/完成等）。"""

    type: Literal["action"] = "action"
    action: Literal["tool_executing", "tool_done"] | str = Field(description="动作标识")
    params: Optional[dict[str, Any]] = Field(default=None, description="动作参数，可缺省")


# ─────────────────── 批次 6 TTS 播放控制（BE-063） ───────────────────

class AudioStartMessage(ContractModel):
    """音频流开始（stream_id = playback ID）；角色字段出现时必须完整。"""
    type: Literal["audio_start"] = "audio_start"
    stream_id: int = Field(description="playback ID")
    format: AudioFormat = Field(default=AudioFormat.WAV, description="音频格式")
    sampleRate: Optional[int] = Field(default=None, description="采样率（WAV header），可缺省")
    durationMs: Optional[int] = Field(default=None, description="时长毫秒（WAV 计算），可缺省")
    conversationId: Optional[str] = Field(default=None, min_length=1, description="会话 ID，可缺省")
    generationId: Optional[str] = Field(default=None, min_length=1, description="生成 ID；预览等非生成音频可缺省")
    characterId: Optional[str] = Field(default=None, min_length=1, description="角色 ID，可缺省")
    characterInstanceId: Optional[str] = Field(default=None, min_length=1, description="角色实例 ID，可缺省")
    characterRevision: Optional[int] = Field(default=None, ge=1, description="角色修订号，可缺省")

    @model_validator(mode="after")
    def _validate_character_identity(self) -> "AudioStartMessage":
        _require_complete_character_identity(
            self.characterId, self.characterInstanceId, self.characterRevision)
        return self



class AudioCancelledMessage(ContractModel):
    """音频流被取消（前端清理孤儿 chunk）。"""
    type: Literal["audio_cancelled"] = "audio_cancelled"
    stream_id: int = Field(description="playback ID")



MAX_ATTACHMENTS_PER_MESSAGE = 20
"""单条消息可携带的附件引用上限（S3-07）。

无上限时，一个 16MB 的 WS 帧可以塞进约十万条引用，服务端**逐条串行** `Mongo`
往返取记录——整条消息循环被独占，其他连接的消息全部排队。
20 是"人类真实用法（几张图/几个文件）"的宽裕上界；超限在**契约层**就被拒
（`ValidationError` → 既有的 `CONTRACT_VALIDATION` 拒绝路径），不会走到取数循环。
"""


class V2TextMessage(ContractModel):
    """v2 客户端文本消息（type=text + 幂等/定位字段；不进 ServerMessage 联合）。

    v2 `content` 恒为 str——媒体/表情走 `attachments`/`stickerId`（批次 7），不沿用
    v1 多模态 content-as-parts 路径。content-as-list 仅 v1 连接（data.get 老路径）
    支持；v2 连接收到 content 非 str（如数组）→ ACK rejected CONTRACT_VALIDATION
    （9abc R1：v2 收紧，媒体必须在 attachments/stickerId 字段携带）。

    handler 按连接 protocol_version 路由：v2 连接解析追加字段，v1 忽略。

    批次 7：加可选 `attachments`（附件引用，对应 POST /api/uploads 上传结果）与
    `stickerId`（内置表情，非附件）；二者与纯文本可共存或独立（sticker/附件-only
    消息触发生成，content 可为空）。
    """

    type: Literal["text"] = "text"
    content: str = Field(description="纯文本")
    clientRequestId: str = Field(description="客户端请求 ID（幂等键，必填）")
    clientMessageId: Optional[str] = Field(default=None, description="客户端本地消息 ID，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，批次 2 忽略/恒 null，可缺省")
    attachments: Optional[list[AttachmentRef]] = Field(
        default=None, max_length=MAX_ATTACHMENTS_PER_MESSAGE,
        description="附件引用列表（POST /api/uploads 上传结果），可缺省；"
                    f"至多 {MAX_ATTACHMENTS_PER_MESSAGE} 条（S3-07）")
    stickerId: Optional[str] = Field(
        default=None, description="内置表情 ID（st_ 前缀，非附件），可缺省")



class InterruptMessage(ContractModel):
    """客户端打断指令。"""

    type: Literal["interrupt"] = "interrupt"



class SetServiceMessage(ContractModel):
    """客户端请求设置服务开关（stt/tts 后端模式）。"""

    type: Literal["set_service"] = "set_service"
    service: Literal[ServiceName.STT, ServiceName.TTS] = Field(description="服务名（仅 stt/tts，不含 llm）")
    enabled: bool = Field(description="是否启用")
    clientRequestId: Optional[str] = Field(
        default=None, description="客户端请求 ID（限流关联回显），可缺省")


# ─────────────────── 批次 6 TTS 播放控制（BE-063） ───────────────────

class AudioCancelMessage(ContractModel):
    """取消在途播放实例（全局操作，v2-only）。"""
    type: Literal["audio.cancel"] = "audio.cancel"
    stream_id: int = Field(description="playback ID")
