"""WS 消息契约：_types 域（ARCH-INC-P2-02 物理拆分）。

装配点见 `frontend_contract/ws_message/__init__.py`；该文件是唯一的
`ServerMessage`/`ClientMessage` 联合定义位置。
"""
from __future__ import annotations


from enum import Enum, StrEnum
from frontend_contract.base import ContractModel, TsNull
from frontend_contract.rest import CharacterBindingSnapshot, CharacterPublicStateView, ConversationUsage, FinishReason
from pydantic import ConfigDict, Discriminator, Field, model_validator
from typing import Annotated, Any, Literal, Optional, Union



class MessageChannel(StrEnum):
    """分段通道（generation.segment.* 与父回复 channel；语义见 batch2 spec §3.2）。"""

    INNER_VOICE = "inner_voice"
    PUBLIC_REPLY = "public_reply"
    STATUS = "status"



class SegmentKind(StrEnum):
    """分段类型（P0 仅 text/markdown 实现，其余预留）。"""

    TEXT = "text"
    MARKDOWN = "markdown"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"
    STICKER = "sticker"
    LINK = "link"



class AudioFormat(StrEnum):
    """音频流格式（audio_end/audio_start 帧）。"""

    WAV = "wav"
    MP3 = "mp3"



class ServiceName(StrEnum):
    """服务名（service_mode_result / set_service 帧）。"""

    STT = "stt"
    TTS = "tts"
    LLM = "llm"



class VoiceEndReason(StrEnum):
    """实时语音会话结束原因（voice_session.ended）。"""

    STOPPED = "stopped"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    STT_DISABLED = "stt_disabled"



class Live2DDirectiveState(StrEnum):
    """后端给 Live2D 的低优先级表现建议状态。"""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    HAPPY = "happy"
    SAD = "sad"
    SURPRISED = "surprised"



class TextPart(ContractModel):
    """文本内容块。"""

    type: Literal["text"] = "text"
    content: str = Field(description="文本内容")



class ImagePart(ContractModel):
    """图片内容块（src 为 URL / data URI / 稳定附件路径）。"""

    type: Literal["image"] = "image"
    src: str = Field(description="图片来源（URL / data URI）")
    mime: Optional[str] = Field(default=None, description="MIME 类型，可缺省")
    detail: Optional[str] = Field(default=None, description="分辨率细节（auto/low/high），可缺省")
    width: Optional[int] = Field(default=None, description="图片宽度 px，可缺省")
    height: Optional[int] = Field(default=None, description="图片高度 px，可缺省")
    attachment_id: Optional[str] = Field(
        default=None, description="附件 ID（att_ 前缀，批次 7 消息关联），可缺省")



class FilePart(ContractModel):
    """文件内容块（wire 镜像 core/schema/part.py FilePart）。"""

    type: Literal["file"] = "file"
    src: str = Field(description="文件来源（稳定附件路径 /api/attachments/{id}）")
    mime: Optional[str] = Field(default=None, description="MIME 类型，可缺省")
    filename: Optional[str] = Field(default=None, description="文件名，可缺省")
    size_bytes: Optional[int] = Field(default=None, description="文件字节数，可缺省")
    attachment_id: Optional[str] = Field(
        default=None, description="附件 ID（att_ 前缀，批次 7 消息关联），可缺省")



class AudioPart(ContractModel):
    """音频内容块（wire 镜像 core/schema/part.py AudioPart）。"""

    type: Literal["audio"] = "audio"
    src: str = Field(description="音频来源（稳定附件路径 /api/attachments/{id}）")
    mime: Optional[str] = Field(default=None, description="MIME 类型，可缺省")
    duration_ms: Optional[int] = Field(default=None, description="音频时长毫秒，可缺省")
    attachment_id: Optional[str] = Field(
        default=None, description="附件 ID（att_ 前缀，批次 7 消息关联），可缺省")



class VideoPart(ContractModel):
    """视频内容块（wire 镜像 core/schema/part.py VideoPart）。"""

    type: Literal["video"] = "video"
    src: str = Field(description="视频来源（稳定附件路径 /api/attachments/{id}）")
    mime: Optional[str] = Field(default=None, description="MIME 类型，可缺省")
    duration_ms: Optional[int] = Field(default=None, description="视频时长毫秒，可缺省")
    attachment_id: Optional[str] = Field(
        default=None, description="附件 ID（att_ 前缀，批次 7 消息关联），可缺省")



class StickerPart(ContractModel):
    """表情内容块（内置公开资源，非上传附件，无 attachment_id）。"""

    type: Literal["sticker"] = "sticker"
    sticker_id: str = Field(description="表情 ID（st_ 前缀）")
    src: str = Field(description="静态资源路径（/static/stickers/{id}.png）")



ContentPart = Annotated[
    Union[TextPart, ImagePart, FilePart, AudioPart, VideoPart, StickerPart],
    Field(discriminator="type"),
]



class ToolCallFunction(ContractModel):
    """工具函数名 + 参数。"""

    name: str = Field(description="函数名")
    arguments: str = Field(description="JSON 字符串参数（不解析）")



class ToolCallData(ContractModel):
    """工具调用数据。"""

    id: str = Field(description="工具调用 ID")
    type: Optional[Literal["function"]] = Field(default=None, description="工具调用类型（function），可缺省")
    function: ToolCallFunction = Field(description="函数名 + JSON 参数")



class ToolCallResult(ContractModel):
    """工具执行结果。"""

    success: Optional[bool] = Field(default=None, description="是否成功（缺省视为 true），可缺省")
    content: Optional[str] = Field(default=None, description="结果内容，可缺省")
    error: Optional[str] = Field(default=None, description="错误信息，可缺省")
    error_type: Optional[str] = Field(default=None, description="错误类型，可缺省")
    duration_ms: Optional[float] = Field(default=None, description="耗时毫秒（可为小数），可缺省")
    truncated: Optional[bool] = Field(default=None, description="结果是否截断，可缺省")



class TokenRound(ContractModel):
    """单轮 token 消耗统计。"""

    model: Optional[str] = Field(default=None, description="模型名，可缺省")
    session_id: Optional[str] = Field(default=None, description="会话 ID，可缺省")
    input_tokens: int = Field(description="输入 token 数")
    output_tokens: int = Field(description="输出 token 数")
    total_tokens: int = Field(description="总 token 数")
    cached_tokens: Optional[int] = Field(default=None, description="缓存 token 数，可缺省")
    reasoning_tokens: Optional[int] = Field(default=None, description="推理 token 数，可缺省")
    round_num: int = Field(description="轮次序号")
    timestamp: Optional[str] = Field(default=None, description="ISO 时间戳，可缺省")



class TokenStats(ContractModel):
    """会话级 token 统计（wire 用 camel byModel，对齐 ConversationUsage，批次 8c F19）。"""

    total: int = Field(description="总 token 数")
    byModel: dict[str, int] = Field(description="按模型拆分的 token 数")



class TokenUsage(ContractModel):
    """token 使用统计（按轮）。"""

    rounds: list[TokenRound] = Field(description="各轮消耗")
    turn_total: int = Field(description="本轮总 token 数")



class TokenTurnStats(ContractModel):
    """TokenUsageMessage.turn 内联形状（rounds + turn_total 均可缺省）。"""

    rounds: Optional[list[TokenRound]] = Field(default=None, description="各轮消耗，可缺省")
    turn_total: Optional[int] = Field(default=None, description="总 token 数，可缺省")



class SessionMessageItem(ContractModel):
    """会话历史中的单条消息（兼容用户/助手/工具结果平铺形状）。"""

    role: str = Field(description="消息角色（user/assistant/tool）")
    message_id: str = Field(min_length=1, description="消息 ID（前端去重/归档去重，必填非空）")
    timestamp: str = Field(min_length=1, description="ISO 时间戳，必填非空")
    parts: Optional[list[dict[str, str]]] = Field(default=None, description="纯文本 parts 列表，可缺省")
    content: Optional[str | list[ContentPart]] = Field(
        default=None, description="消息内容（纯文本或内容块列表），可缺省")
    content_type: Optional[dict[str, list[int]]] = Field(
        default=None, description="内容类型 → 序号映射，可缺省")
    mood: Optional[str] = Field(default=None, description="情绪标签，可缺省")
    tool_calls: Optional[list[ToolCallData]] = Field(default=None, description="工具调用列表，可缺省")
    inner_voice: Optional[str] = Field(default=None, description="内心独白，可缺省")
    tool_call_id: Optional[str] = Field(default=None, description="工具调用 ID（tool 角色），可缺省")
    name: Optional[str] = Field(default=None, description="工具名（tool 角色），可缺省")
    success: Optional[bool] = Field(default=None, description="工具是否成功，可缺省")
    error: Optional[str] = Field(default=None, description="工具错误信息，可缺省")
    error_type: Optional[str] = Field(default=None, description="工具错误类型，可缺省")
    duration_ms: Optional[float] = Field(default=None, description="工具耗时毫秒（可为小数），可缺省")
    truncated: Optional[bool] = Field(default=None, description="内容是否截断，可缺省")
    token_usage: Optional[TokenUsage] = Field(default=None, description="消息附带的 token 统计，可缺省")
    character: Optional[CharacterBindingSnapshot] = Field(
        default=None,
        description="assistant 实际生成角色的不可变审计快照；user/tool 消息缺省",
    )



class ServiceCapability(ContractModel):
    """单服务运行时能力（available=false 时 model 为 null）。"""

    available: bool = Field(description="后端连接可用")
    enabled: bool = Field(description="用户模式")
    model: Annotated[Optional[str], TsNull()] = Field(
        default=None, description="当前模型，unavailable 时为 null")
    default: str = Field(description="config.yaml 默认模型")



class AttachmentRef(ContractModel):
    """v2 文本消息携带的附件引用（发送后随 user_message.ack 回显确认）。

    kind/mime 为**客户端提示值**，服务端以 attachments 集合记录为准（Minor 10，
    不校验提示与记录一致——校验失败会误拒合法文件，展示用记录值）。
    """

    attachmentId: str = Field(description="附件 ID（att_ 前缀，POST /api/uploads 返回）")
    kind: str = Field(description="附件类别提示值（image/video/audio/file）")
    mime: str = Field(description="MIME 类型提示值")



class ServerInfoFeatures(ContractModel):
    """能力特征声明。严禁超前声明——每个字段必须如实反映后端当前真实能力。"""

    structured_generation: bool = Field(default=False, description="结构化聊天（generation.* 事件，批次 2）")
    multi_conversation: bool = Field(default=False, description="多会话（批次 3）")
    desktop_runtime: bool = Field(default=False, description="Desktop 工具 Runtime（批次 4）")
    dictation: bool = Field(default=False, description="输入法转写（批次 6）")
    voice_recording: bool = Field(default=False, description="普通录音（批次 6）")
    voice_realtime: bool = Field(default=False, description="实时语音会话（批次 6）")
    media_upload: bool = Field(default=False, description="附件上传（批次 7，POST /api/uploads）")
    idempotency_durable: bool = Field(default=False, description="幂等 claim 跨重启持久化（D6c，true=durable / false=内存降级非 durable 窗口）")
    character_profiles: bool = Field(
        default=False,
        description="多角色 Profile（Stage H 门禁完成后由服务端如实开启）",
    )
    character_state_stream: bool = Field(default=False, description="角色状态流（阶段 G；character.state.changed 事件；character_profiles=false 时独立可用）")



class AckError(ContractModel):
    """rejected ACK 错误负载（取代任意 dict；details 可携带 idempotency_durable，D3）。

    与 GenerationFailedMessage/ErrorMessage 的错误信封同位置字段（code/message/
    retryable/details）+ 顶层 retryAfterMs（与 ErrorMessage 同位置，§2.1）。
    """

    code: str = Field(description="稳定错误码（CONTRACT_VALIDATION/SESSION_NOT_FOUND/QUEUE_FULL/STORAGE_UNAVAILABLE/RATE_LIMITED/IN_PROGRESS/INTERNAL 等）")
    message: str = Field(description="公开说明")
    retryable: bool = Field(description="是否可重试")
    details: Optional[dict[str, Any]] = Field(
        default=None, description="公开安全详情（STORAGE_UNAVAILABLE 时携带 idempotency_durable），可缺省")
    retryAfterMs: Optional[int] = Field(
        default=None, description="建议重试退避毫秒（retryable 时），可缺省")



class AcceptedUserAckMessage(ContractModel):
    """用户消息接受确认（v2）：accepted → 后续 generation.* 事件；无 error。

    extra=forbid：accepted 带 error 键即非法组合（判别联合不变量，机器契约拒绝）。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["user_message.ack"] = "user_message.ack"
    messageId: str = Field(min_length=1, description="正式用户消息 ID（预分配，落库一致）")
    clientRequestId: str = Field(min_length=1, description="原客户端请求 ID")
    clientMessageId: Optional[str] = Field(default=None, description="客户端本地消息 ID，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")
    receivedAt: str = Field(description="接收时间（UTC ISO）")
    status: Literal["accepted"] = Field(description="判别键：accepted 恒字面量")



class RejectedUserAckMessage(ContractModel):
    """用户消息拒绝确认（v2）：messageId 恒 ""（D2：短期保留空白串兼容前端批次 A）；error 必填。

    clientRequestId 不做 min_length——「缺少 clientRequestId」的契约拒绝需要回显空串
    （否则构造失败被吞，客户端拿不到任何响应）。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["user_message.ack"] = "user_message.ack"
    messageId: Literal[""] = Field(default="", description="正式用户消息 ID——rejected 恒空串（D2）")
    clientRequestId: str = Field(description="原客户端请求 ID（缺失时回显空串）")
    clientMessageId: Optional[str] = Field(default=None, description="客户端本地消息 ID，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")
    receivedAt: str = Field(description="接收时间（UTC ISO）")
    status: Literal["rejected"] = Field(description="判别键：rejected 恒字面量")
    error: AckError = Field(description="拒绝原因（必填）")



def _require_complete_character_identity(
    character_id: str | None,
    character_instance_id: str | None,
    character_revision: int | None,
) -> None:
    """兼容三字段全缺省，但拒绝只出现其中一部分的 generation 身份。"""
    present = (
        character_id is not None,
        character_instance_id is not None,
        character_revision is not None,
    )
    if any(present) and not all(present):
        raise ValueError(
            "generation character identity must provide characterId, "
            "characterInstanceId and characterRevision together"
        )



class TextMessage(ContractModel):
    """客户端发送文本/多模态消息。"""

    type: Literal["text"] = "text"
    content: str | list[ContentPart] = Field(description="纯文本或内容块列表")



class AudioInputDescriptor(ContractModel):
    """客户端输入音频描述（9C-1）：固定格式，仅支持 pcm_s16le/16000Hz/单声道。

    三种语音 start 帧（voice.record.start / voice_session.start / dictation.start）
    的 `audio` 字段；缺省 → 默认 16k/mono/pcm_s16le（向后兼容）。语义校验
    （codec/sampleRate/channels 唯一支持值）由 VoiceSessionManager 负责。
    """

    codec: str = Field(description="编码（唯一支持 pcm_s16le）")
    sampleRate: int = Field(description="采样率（唯一支持 16000）")
    channels: int = Field(description="声道数（唯一支持 1）")



class ToolRuntime(StrEnum):
    """工具 Runtime（wire 值 backend/desktop/local_worker；D6-03 权威键关联）。"""

    BACKEND = "backend"
    DESKTOP = "desktop"
    LOCAL_WORKER = "local_worker"



class ToolStatusEnum(str, Enum):
    """桌面工具状态（Client→Server `tool.status` 判别六态，D-m）。

    str Enum 经 gen_ts 发射为 TS 判别联合（export type ToolStatusEnum = 'accepted' | ...）；
    `ToolStatusMessage.status` 字段注解引用本枚举 → pydantic 校验 + wire 序列化为字符串值。
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RUNNING = "running"
    RESULT = "result"
    ERROR = "error"
    CANCELLED = "cancelled"



class ToolAttachment(ContractModel):
    """桌面工具结果附件引用（后端托管 URL）。

    Electron 先把附件字节经 REST `POST /api/uploads/desktop/{callId}` 上传落盘，
    拿到 url 后再在 `tool.status{status=result}` 的 attachments 里回填（D-d）。
    """

    url: str = Field(description="附件 URL（后端托管 /uploads/...）")
    mimeType: str = Field(description="MIME 类型")
    label: Optional[str] = Field(default=None, description="附件标签，可缺省")
