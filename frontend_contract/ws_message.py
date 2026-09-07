"""WS 消息契约建模：Server→Client 38 具名成员 + Client→Server 消息 + 支持类型（零依赖）。

对照 `frontend/src/types/protocol.ts` 逐字段对齐；ts_null vs X? 按发送点实际 wire 定性：
  - switch_model_result.error：`**result` 展开，成功无键 → Optional[str]（X?）
  - service_mode_result.error：`result.get("error")` 恒写 → Annotated[Optional[str], TsNull()]
  - CapabilitiesMessage.services.{stt,tts,llm}.model：available=false 时显式 null → TsNull
  - 其余 Optional 字段（stream_id/error?/format? 等）默认 X?（缺省省略）
判别联合别名 `ServerMessage` 模块级可访问（__main__ 经 get_args 收集成员）。
"""
from enum import Enum, StrEnum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import ConfigDict, Discriminator, Field, model_validator

from frontend_contract.base import ContractModel, TsNull
from frontend_contract.rest import (
    CharacterBindingSnapshot,
    CharacterPublicStateView,
    ConversationUsage,
    FinishReason,
)
from frontend_contract.version import PROTOCOL_VERSION

# ────────────────────── 非判别受限字段枚举（plan Task 6，StrEnum） ──────────────────────


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

# ───────────────────────────── 支持类型 ─────────────────────────────


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


# ─────────────────────────── 服务端→客户端 ───────────────────────────


class SessionMessage(ContractModel):
    """会话历史回放（新连接建立时发送）。conversationId = 本回放消息所属会话。"""

    type: Literal["session"] = "session"
    conversationId: str = Field(description="本回放消息所属会话 ID")
    messages: list[SessionMessageItem] = Field(description="会话消息列表")
    token_stats: Optional[TokenStats] = Field(default=None, description="会话级 token 统计，可缺省")


class CapabilitiesMessage(ContractModel):
    """服务能力声明（建连后发送，供前端回填设置面板下拉选中值）。"""

    type: Literal["capabilities"] = "capabilities"
    live2d_static_image: str = Field(description="Live2D 静态图路径")
    services: dict[str, ServiceCapability] = Field(description="llm/stt/tts 三服务能力")


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
    character_profiles: bool = Field(default=False, description="多角色 Profile（阶段 C+E；本批次恒 false，架构稿决策 #12）")
    character_state_stream: bool = Field(default=False, description="角色状态流（阶段 G；character.state.changed 事件；character_profiles=false 时独立可用）")


class ServerInfoMessage(ContractModel):
    """协议版本 + 服务器能力声明（仅 v2 客户端；取代 capabilities，避免重复帧）。

    `services` 复用 per-service 形状（available/enabled/model/default），
    Electron 设置面板与 v1 共用渲染逻辑。
    `connectionId`（批次 4）：桌面客户端连接 ID（Electron 附件上传 `X-Client-Connection`
    头的取值来源，BE-042 持有者校验闭环）；仅 v2 + electron 建连时下发，
    非 electron/普通 v2 恒省略（None → wire 缺省省略，v1 类型字节不变）。
    """

    type: Literal["server_info"] = "server_info"
    protocol_version: str = Field(default=PROTOCOL_VERSION, description="协商的协议版本")
    server_version: str = Field(description="服务器版本（core/version.py，含 +build_sha 后缀）")
    features: ServerInfoFeatures = Field(description="能力特征（严禁超前声明）")
    services: dict[str, ServiceCapability] = Field(description="llm/stt/tts 三服务能力")
    connectionId: Optional[str] = Field(
        default=None, description="桌面客户端连接 ID（仅 v2 + electron 下发），可缺省")
    build_sha: Optional[str] = Field(
        default=None, description="构建 git SHA（core/version.py get_build_sha，失败缺省省略），可缺省")


# ─────────────────────────── 批次 2 v2 generation 事件 ───────────────────────────

# 分段通道 / 分段类型 / 完成原因 / ACK 状态——语义见 batch2 spec §3.2（kind 仅 text/markdown 实现，其余预留）


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


# 模块级命名判别联合（Python 侧类型用；ServerMessage 的 ack 分支引用它，生成器递归展开）
UserAckMessage = Annotated[
    Union[AcceptedUserAckMessage, RejectedUserAckMessage],
    Discriminator("status"),
]


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


class CharacterStateChangedMessage(ContractModel):
    """角色状态变化事件(架构稿 §12.4;v2,character_state_stream=true 后发送)。

    只含产品态字段;state 是完整 CharacterPublicStateView,不是 patch;未知 optional 字段省略。
    客户端只按同一 instance 内严格递增的 stateVersion 应用事件,不能用时间字符串替代顺序。
    """
    type: Literal["character.state.changed"] = "character.state.changed"
    conversationId: str = Field(min_length=1, description="会话 ID")
    characterId: str = Field(description="角色 ID")
    characterInstanceId: str = Field(description="角色实例 ID")
    characterRevision: int = Field(default=1, description="角色修订号")
    stateVersion: int = Field(default=1, description="实例状态版本")
    state: "CharacterPublicStateView" = Field(description="完整产品态视图(非 patch)")


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


class CommandMessage(ContractModel):
    """前端指令（工具审批弹窗 / 阻塞 / 内心独白）。"""

    type: Literal["command"] = "command"
    command: Literal["tool_approval", "block", "inner_voice"] | str = Field(description="指令标识")
    params: Optional[dict[str, Any]] = Field(default=None, description="指令参数，可缺省")


class WakeMessage(ContractModel):
    """唤醒词检测通知。"""

    type: Literal["wake"] = "wake"


class SwitchModelResultMessage(ContractModel):
    """模型切换结果。error 仅失败带（成功缺省省略 → 不用 ts_null）。"""

    type: Literal["switch_model_result"] = "switch_model_result"
    service: str = Field(description="服务名（llm/stt/tts）")
    model: str = Field(description="目标模型标识")
    ok: bool = Field(description="切换是否成功")
    error: Optional[str] = Field(default=None, description="失败原因，可缺省")


class ServiceModeResultMessage(ContractModel):
    """服务模式开关结果。error 恒存在（成功为 null → 用 ts_null）。"""

    type: Literal["service_mode_result"] = "service_mode_result"
    service: ServiceName = Field(description="服务名")
    enabled: bool = Field(description="有效模式（永远随消息出现）")
    ok: bool = Field(description="操作是否成功")
    error: Annotated[Optional[str], TsNull()] = Field(default=None, description="错误，无则 null")


class SetTtsVoiceResultMessage(ContractModel):
    """TTS 声音设置结果（Server→Client，响应 set_tts_voice）。

    成功带 voice 回显、失败带 error（均缺省省略）；与 switch_model_result/service_mode_result 同列——
    加入 ServerMessage 判别联合，否则 gen_ts 不生成 TS 判别。恒作用于活动会话。
    """

    type: Literal["set_tts_voice_result"] = "set_tts_voice_result"
    ok: bool = Field(description="设置是否成功")
    voice: Optional[str] = Field(default=None, description="生效的声音 ID（成功回显），可缺省")
    error: Optional[str] = Field(default=None, description="失败原因（校验失败/后端不可用），可缺省")


class ErrorMessage(ContractModel):
    """错误消息。

    requestId（批次 7，P7-12）：关联 WS 侧请求 ID（来源 UserInputData.request_id，
    有则带、无则 None 缺省省略）——客户端可凭此关联日志/幂等排查。
    clientRequestId（批次 B，BE-P1-03）：关联客户端请求 ID——命令类消息限流时提取
    回显，前端可凭此 settle 对应 promise。
    retryAfterMs（批次 B）：顶层建议重试退避毫秒（RATE_LIMITED 时），可缺省。
    """

    type: Literal["error"] = "error"
    message: str = Field(description="错误描述")
    code: Optional[str] = Field(default=None, description="错误码（如 SESSION_NOT_FOUND），可缺省")
    requestId: Optional[str] = Field(default=None, description="关联请求 ID（WS 侧 request_id），可缺省")
    clientRequestId: Optional[str] = Field(
        default=None, description="关联客户端请求 ID（命令类限流提取回显），可缺省")
    retryable: Optional[bool] = Field(default=None, description="是否可重试，可缺省")
    retryAfterMs: Optional[int] = Field(
        default=None, description="建议重试退避毫秒（RATE_LIMITED 时），可缺省")
    details: Optional[dict[str, Any]] = Field(default=None, description="公开安全详情，可缺省")


# ─────────────────── 批次 6 三种语音（BE-060/061/062） ───────────────────

# 服务端→客户端


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


# ─────────────────── 批次 6 TTS 播放控制（BE-063） ───────────────────

class AudioStartMessage(ContractModel):
    """音频流开始（stream_id = playback ID）。"""
    type: Literal["audio_start"] = "audio_start"
    stream_id: int = Field(description="playback ID")
    format: AudioFormat = Field(default=AudioFormat.WAV, description="音频格式")
    sampleRate: Optional[int] = Field(default=None, description="采样率（WAV header），可缺省")
    durationMs: Optional[int] = Field(default=None, description="时长毫秒（WAV 计算），可缺省")


class AudioCancelledMessage(ContractModel):
    """音频流被取消（前端清理孤儿 chunk）。"""
    type: Literal["audio_cancelled"] = "audio_cancelled"
    stream_id: int = Field(description="playback ID")


# ─────────────────────────── 客户端→服务器 ───────────────────────────


class TextMessage(ContractModel):
    """客户端发送文本/多模态消息。"""

    type: Literal["text"] = "text"
    content: str | list[ContentPart] = Field(description="纯文本或内容块列表")


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
        default=None, description="附件引用列表（POST /api/uploads 上传结果），可缺省")
    stickerId: Optional[str] = Field(
        default=None, description="内置表情 ID（st_ 前缀，非附件），可缺省")


class InterruptMessage(ContractModel):
    """客户端打断指令。"""

    type: Literal["interrupt"] = "interrupt"


class SwitchModelMessage(ContractModel):
    """客户端请求切换模型。"""

    type: Literal["switch_model"] = "switch_model"
    service: str = Field(description="服务名（llm/stt/tts）")
    model: str = Field(description="目标模型标识")
    clientRequestId: Optional[str] = Field(
        default=None, description="客户端请求 ID（限流关联回显），可缺省")


class SetServiceMessage(ContractModel):
    """客户端请求设置服务开关（stt/tts 后端模式）。"""

    type: Literal["set_service"] = "set_service"
    service: Literal[ServiceName.STT, ServiceName.TTS] = Field(description="服务名（仅 stt/tts，不含 llm）")
    enabled: bool = Field(description="是否启用")
    clientRequestId: Optional[str] = Field(
        default=None, description="客户端请求 ID（限流关联回显），可缺省")


class SetTtsVoiceMessage(ContractModel):
    """客户端设置 TTS 声音（v2，恒作用于活动会话，BE-051）。

    服务端经活动会话激活 TTS 后端 `list_voices()` 校验 voice 存在后写入 service_state.tts_voice；
    未知 voice/后端不可用 → set_tts_voice_result {ok: False, error}（不改 state）。
    """

    type: Literal["set_tts_voice"] = "set_tts_voice"
    voice: str = Field(description="目标声音 ID（须在活动会话激活 TTS 后端 list_voices 结果内）")
    clientRequestId: Optional[str] = Field(
        default=None, description="客户端请求 ID（限流关联回显），可缺省")


class ToolApprovalMessage(ContractModel):
    """客户端提交工具审批结果。"""

    type: Literal["tool_approval"] = "tool_approval"
    request_id: str = Field(description="审批请求 ID")
    approved: bool = Field(description="是否批准")
    clientRequestId: Optional[str] = Field(
        default=None, description="客户端请求 ID（限流关联回显），可缺省")


class WakeToggleMessage(ContractModel):
    """客户端切换唤醒词开关。"""

    type: Literal["wake_toggle"] = "wake_toggle"
    enabled: bool = Field(description="是否启用")
    clientRequestId: Optional[str] = Field(
        default=None, description="客户端请求 ID（限流关联回显），可缺省")


class ActivateSessionMessage(ContractModel):
    """客户端激活/切换会话（v2）：服务端切 active 并重发新活动会话回放。

    未知 conversationId → 不回放，发 ErrorMessage{code: SESSION_NOT_FOUND}（批次 1 错误信封）。
    """

    type: Literal["activate_session"] = "activate_session"
    conversationId: str = Field(description="目标会话 ID（必填）")
    clientRequestId: Optional[str] = Field(
        default=None, description="客户端请求 ID（限流关联回显），可缺省")


# ─────────────────── 批次 6 三种语音（BE-060/061/062） ───────────────────

# 客户端→服务器


class AudioInputDescriptor(ContractModel):
    """客户端输入音频描述（9C-1）：固定格式，仅支持 pcm_s16le/16000Hz/单声道。

    三种语音 start 帧（voice.record.start / voice_session.start / dictation.start）
    的 `audio` 字段；缺省 → 默认 16k/mono/pcm_s16le（向后兼容）。语义校验
    （codec/sampleRate/channels 唯一支持值）由 VoiceSessionManager 负责。
    """

    codec: str = Field(description="编码（唯一支持 pcm_s16le）")
    sampleRate: int = Field(description="采样率（唯一支持 16000）")
    channels: int = Field(description="声道数（唯一支持 1）")


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


# ─────────────────── 批次 6 TTS 播放控制（BE-063） ───────────────────

class AudioCancelMessage(ContractModel):
    """取消在途播放实例（全局操作，v2-only）。"""
    type: Literal["audio.cancel"] = "audio.cancel"
    stream_id: int = Field(description="playback ID")


# ─────────────────────────── 批次 4 Desktop 工具桥（BE-040/041/043） ───────────────────────────


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


class ToolRequestMessage(ContractModel):
    """桌面工具执行请求（Server→Client，v2 定向投递 desktop channel）。

    bridge._deliver 按 call 登记的 `target_connection` 精确定位
    `client_platform == "electron"` 且 `connection_id` 相等的 v2 连接，再 _send_json
    本帧；v1/其它 Electron 连接不收 desktop 帧（D-l）。paramHash = 规范化 arguments
    的 SHA-256（BE-043 篡改拒绝权威依据）；expiresAt = 桥 request_timeout 的绝对
    过期时间（UTC ISO）。
    """

    type: Literal["tool.request"] = "tool.request"
    callId: str = Field(description="调用 ID（call_ 前缀）")
    toolName: str = Field(description="工具名（desktop.file.read 等）")
    toolVersion: str = Field(description="工具版本（语义化版本）")
    runtime: ToolRuntime = Field(description="工具 Runtime（desktop）")
    sourceCallId: str = Field(description="LLM tool_call_id（前端统一领域键，D6-03）")
    arguments: dict[str, Any] = Field(description="工具参数（input_schema 校验后）")
    reason: Optional[str] = Field(default=None, description="调用原因（LLM 生成），可缺省")
    riskLevel: str = Field(description="风险等级（safe/medium/high）")
    permissionRequired: bool = Field(description="是否需要权限（risk != safe 或 requires_human_approval）")
    approvalAuthority: str = Field(description="审批主体（desktop → client，D-h）")
    paramHash: str = Field(description="参数摘要（SHA-256，规范化 JSON，篡改拒绝依据）")
    expiresAt: str = Field(description="过期时间（UTC ISO）")
    requestId: Optional[str] = Field(default=None, description="关联请求 ID（批次 1 错误信封惯例），可缺省")


class ToolStatusMessage(ContractModel):
    """桌面工具状态回传（Client→Server，判别消息六态，D-m）。

    accepted/running：桥仅更新 repo（Electron 已接手/执行中）；
    result/error/rejected/cancelled：桥 resolve 对应 future（§4.3 状态→ToolResult 映射）。
    回传 toolName/toolVersion/runtime/paramHash 与登记不一致 → 桥拒绝（BE-043 篡改防护）。
    """

    type: Literal["tool.status"] = "tool.status"
    callId: str = Field(description="调用 ID（call_ 前缀）")
    status: ToolStatusEnum = Field(description="状态（accepted/rejected/running/result/error/cancelled）")
    toolName: str = Field(description="工具名")
    toolVersion: str = Field(description="工具版本")
    runtime: str = Field(description="工具 Runtime（desktop）")
    paramHash: str = Field(description="参数摘要（SHA-256，回传一致性校验）")
    output: Optional[str] = Field(default=None, description="公开输出文本（status=result），可缺省")
    error: Optional[dict[str, Any]] = Field(default=None, description="错误（{code,message}，status=error），可缺省")
    attachments: Optional[list[ToolAttachment]] = Field(
        default=None, description="附件引用（status=result，后端托管 URL），可缺省")
    executionSummary: Optional[str] = Field(default=None, description="执行摘要（去本地路径），可缺省")


class ToolStartedMessage(ContractModel):
    """Backend 工具开始（v2，Server→Client 展示生命周期，D6-02/08）。"""

    type: Literal["tool.started"] = "tool.started"
    callId: str = Field(description="LLM tool_call_id（归档同值，前端权威键）")
    toolName: str = Field(description="工具名")
    runtime: ToolRuntime = Field(description="工具 Runtime")
    inputSummary: Optional[str] = Field(default=None, description="服务端脱敏参数摘要（_safe_arg_summary），可缺省")
    generationId: Optional[str] = Field(default=None, description="生成实例 ID，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")


class ToolCompletedMessage(ContractModel):
    """Backend 工具完成（v2，Server→Client 展示生命周期，D6-02/08）。"""

    type: Literal["tool.completed"] = "tool.completed"
    callId: str = Field(description="LLM tool_call_id（与 started 同值）")
    toolName: str = Field(description="工具名")
    runtime: ToolRuntime = Field(description="工具 Runtime")
    success: bool = Field(description="是否成功")
    outputSummary: Optional[str] = Field(default=None, description="截断后的公开结果摘要（≤2000 字符），可缺省")
    error: Optional[dict[str, Any]] = Field(default=None, description="错误（{code,message}，code=errorType 或 TOOL_ERROR），可缺省")
    errorType: Optional[str] = Field(default=None, description="ErrorType 值，可缺省")
    durationMs: Optional[float] = Field(default=None, description="耗时毫秒，可缺省")
    truncated: Optional[bool] = Field(default=None, description="outputSummary 超长截断标记，可缺省")
    generationId: Optional[str] = Field(default=None, description="生成实例 ID，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")


# ─────────────────── ClientMessage 判别联合（模块级，9B-2） ───────────────────

# 客户端→服务器入站消息判别联合：v2 连接 handle_json_message 入口统一
# TypeAdapter(ClientMessage).validate_python 校验（未知 type/错误枚举/字段类型/缺必填
# → ErrorMessage CONTRACT_VALIDATION）。**不含 `TextMessage`**——其 `type="text"` 与
# `V2TextMessage` 撞 discriminator，pydantic 判别联合无法共存（定义即抛 TypeError）；
# `type="text"` 由 `V2TextMessage` 独占，v1 多模态 text 走 data.get 老路径。

ClientMessage = Annotated[
    Union[
        V2TextMessage, InterruptMessage, SwitchModelMessage, SetServiceMessage,
        SetTtsVoiceMessage, ToolApprovalMessage, WakeToggleMessage, ActivateSessionMessage,
        VoiceRecordStartMessage, VoiceRecordStopMessage, VoiceCommitMessage, VoiceDiscardMessage,
        VoiceSessionStartMessage, VoiceSessionPauseMessage, VoiceSessionResumeMessage,
        VoiceSessionStopMessage, VoiceSessionCancelMessage, DictationStartMessage,
        DictationStopMessage, DictationCancelMessage, AudioCancelMessage, ToolStatusMessage,
    ],
    Field(discriminator="type"),
]


# ─────────────────── ServerMessage 判别联合（模块级） ───────────────────

ServerMessage = Annotated[Union[
    SessionMessage,
    CapabilitiesMessage,
    ServerInfoMessage,
    DeltaMessage,
    MessageMessage,
    TokenUsageMessage,
    ExpressionMessage,
    Live2DDirectiveMessage,
    StateMessage,
    AudioMessage,
    AudioEndMessage,
    TypingMessage,
    TranscriptionMessage,
    ActionMessage,
    ToolStartedMessage,
    ToolCompletedMessage,
    CommandMessage,
    WakeMessage,
    SwitchModelResultMessage,
    ServiceModeResultMessage,
    SetTtsVoiceResultMessage,
    ErrorMessage,
    UserAckMessage,
    GenerationStartedMessage,
    GenerationSegmentStartedMessage,
    GenerationDeltaMessage,
    GenerationSegmentCompletedMessage,
    GenerationCompletedMessage,
    GenerationFailedMessage,
    ToolRequestMessage,
    # 阶段 G(CharacterPublicState 实际发送)
    CharacterStateChangedMessage,
    # 批次 6 三种语音（BE-060/061/062）
    VoiceTranscriptMessage,
    VoiceSessionStartedMessage,
    VoiceSessionPartialMessage,
    VoiceSessionTurnMessage,
    VoiceSessionAckMessage,
    VoiceSessionEndedMessage,
    DictationPartialMessage,
    DictationFinalMessage,
    DictationCancelledMessage,
    # 批次 6 TTS 播放控制（BE-063）
    AudioStartMessage,
    AudioCancelledMessage,
], Field(discriminator="type")]
