"""WS 消息契约：voice 域（ARCH-INC-P2-02 物理拆分）。

装配点见 `frontend_contract/ws_message/__init__.py`；该文件是唯一的
`ServerMessage`/`ClientMessage` 联合定义位置。
"""
from __future__ import annotations


from frontend_contract.base import ContractModel, TsNull
from pydantic import ConfigDict, Discriminator, Field, model_validator
from typing import Annotated, Any, Literal, Optional, Union
from frontend_contract.ws_message._types import AudioInputDescriptor, VoiceEndReason



class VoiceTranscriptMessage(ContractModel):
    """普通录音预转写字幕（stop 后、commit 前）。"""
    type: Literal["voice.transcript"] = "voice.transcript"
    recordingId: str = Field(description="录音 ID")
    text: str = Field(description="字幕文本")
    emotion: Optional[str] = Field(default=None, description="情绪标签，可缺省")



class VoiceSessionStartedMessage(ContractModel):
    """实时语音会话已开始（conversationId = 解析后实际目标会话）。"""
    type: Literal["voice_session.started"] = "voice_session.started"
    sessionId: str = Field(description="会话 ID")
    conversationId: Optional[str] = Field(default=None, description="实际目标会话，可缺省")



class VoiceSessionPartialMessage(ContractModel):
    """实时语音轮次字幕（每段一条 = 该轮 final 文本，仅显示）。"""
    type: Literal["voice_session.partial"] = "voice_session.partial"
    sessionId: str = Field(description="会话 ID")
    turnId: int = Field(description="轮次序号（每轮单调递增）")
    segmentIndex: int = Field(description="全局分段序号（跨轮累计）")
    text: str = Field(description="该轮文本")



class VoiceSessionTurnMessage(ContractModel):
    """实时语音轮次已提交（自动触发 generation）。"""
    type: Literal["voice_session.turn"] = "voice_session.turn"
    sessionId: str = Field(description="会话 ID")
    turnId: int = Field(description="轮次序号")
    messageId: str = Field(description="用户消息 ID（预分配，落库一致）")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")
    text: str = Field(description="该轮最终文本")



class VoiceSessionAckMessage(ContractModel):
    """实时语音背压进度通知（seq = 服务端已接收 BINARY 帧序数）。"""
    type: Literal["voice_session.ack"] = "voice_session.ack"
    sessionId: str = Field(description="会话 ID")
    seq: int = Field(description="已接收帧序数")



class VoiceSessionEndedMessage(ContractModel):
    """实时语音会话结束。"""
    type: Literal["voice_session.ended"] = "voice_session.ended"
    sessionId: str = Field(description="会话 ID")
    reason: VoiceEndReason = Field(description="结束原因")



class DictationPartialMessage(ContractModel):
    """输入法转写中间字幕（仅显示）。"""
    type: Literal["dictation.partial"] = "dictation.partial"
    text: str = Field(description="累积文本")



class DictationFinalMessage(ContractModel):
    """输入法转写最终结果（只回文字，零消息副作用）。"""
    type: Literal["dictation.final"] = "dictation.final"
    clientRequestId: str = Field(description="原请求 ID")
    text: str = Field(description="最终文字")



class DictationCancelledMessage(ContractModel):
    """输入法转写已取消。"""
    type: Literal["dictation.cancelled"] = "dictation.cancelled"
    clientRequestId: str = Field(description="原请求 ID")



class VoiceRecordStartMessage(ContractModel):
    """开始普通录音（全段缓冲，不触发）。"""
    type: Literal["voice.record.start"] = "voice.record.start"
    recordingId: str = Field(description="录音 ID（客户端生成 rec_ 前缀）")
    audio: Optional[AudioInputDescriptor] = Field(
        default=None, description="输入音频描述（缺省按 16k/mono/pcm_s16le）")



class VoiceRecordStopMessage(ContractModel):
    """停止普通录音并预转写。"""
    type: Literal["voice.record.stop"] = "voice.record.stop"
    recordingId: str = Field(description="录音 ID")



class VoiceCommitMessage(ContractModel):
    """确认普通录音提交（v2：幂等 + 预分配 messageId）。text = 用户确认的字幕文本（幂等摘要依据）。"""
    type: Literal["voice.commit"] = "voice.commit"
    recordingId: str = Field(description="录音 ID")
    text: str = Field(description="确认的字幕文本")
    clientRequestId: str = Field(description="幂等键（必填）")
    clientMessageId: Optional[str] = Field(default=None, description="客户端本地消息 ID，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")



class VoiceDiscardMessage(ContractModel):
    """丢弃普通录音。"""
    type: Literal["voice.discard"] = "voice.discard"
    recordingId: str = Field(description="录音 ID")



class VoiceSessionStartMessage(ContractModel):
    """开始实时语音会话。"""
    type: Literal["voice_session.start"] = "voice_session.start"
    sessionId: str = Field(description="会话 ID（客户端生成 vs_ 前缀）")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")
    audio: Optional[AudioInputDescriptor] = Field(
        default=None, description="输入音频描述（缺省按 16k/mono/pcm_s16le）")



class VoiceSessionPauseMessage(ContractModel):
    type: Literal["voice_session.pause"] = "voice_session.pause"
    sessionId: str = Field(description="会话 ID")



class VoiceSessionResumeMessage(ContractModel):
    type: Literal["voice_session.resume"] = "voice_session.resume"
    sessionId: str = Field(description="会话 ID")



class VoiceSessionStopMessage(ContractModel):
    type: Literal["voice_session.stop"] = "voice_session.stop"
    sessionId: str = Field(description="会话 ID")



class VoiceSessionCancelMessage(ContractModel):
    type: Literal["voice_session.cancel"] = "voice_session.cancel"
    sessionId: str = Field(description="会话 ID")



class DictationStartMessage(ContractModel):
    type: Literal["dictation.start"] = "dictation.start"
    clientRequestId: str = Field(description="客户端请求 ID")
    sttProfileId: Optional[str] = Field(
        default=None, description="STT profile（<provider>/<model>，缺省用全局活动 STT）")
    audio: Optional[AudioInputDescriptor] = Field(
        default=None, description="输入音频描述（缺省按 16k/mono/pcm_s16le）")



class DictationStopMessage(ContractModel):
    type: Literal["dictation.stop"] = "dictation.stop"
    clientRequestId: str = Field(description="请求 ID")



class DictationCancelMessage(ContractModel):
    type: Literal["dictation.cancel"] = "dictation.cancel"
    clientRequestId: str = Field(description="请求 ID")
