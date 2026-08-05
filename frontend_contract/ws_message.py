"""WS 消息契约建模：Server→Client 18 具名成员 + Client→Server 消息 + 支持类型（零依赖）。

对照 `frontend/src/types/protocol.ts` 逐字段对齐；ts_null vs X? 按发送点实际 wire 定性：
  - switch_model_result.error：`**result` 展开，成功无键 → Optional[str]（X?）
  - service_mode_result.error：`result.get("error")` 恒写 → Annotated[Optional[str], TsNull()]
  - CapabilitiesMessage.services.{stt,tts,llm}.model：available=false 时显式 null → TsNull
  - 其余 Optional 字段（stream_id/error?/format? 等）默认 X?（缺省省略）
判别联合别名 `ServerMessage` 模块级可访问（__main__ 经 get_args 收集成员）。
"""
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import Field

from frontend_contract.base import ContractModel, TsNull
from frontend_contract.rest import ConversationUsage

# ───────────────────────────── 支持类型 ─────────────────────────────


class TextPart(ContractModel):
    """文本内容块。"""

    type: Literal["text"] = "text"
    content: str = Field(description="文本内容")


class ImagePart(ContractModel):
    """图片内容块（src 为 URL 或 data URI）。"""

    type: Literal["image"] = "image"
    src: str = Field(description="图片来源（URL / data URI）")
    mime: Optional[str] = Field(default=None, description="MIME 类型，可缺省")
    detail: Optional[str] = Field(default=None, description="分辨率细节（auto/low/high），可缺省")
    width: Optional[int] = Field(default=None, description="图片宽度 px，可缺省")
    height: Optional[int] = Field(default=None, description="图片高度 px，可缺省")


ContentPart = Annotated[Union[TextPart, ImagePart], Field(discriminator="type")]


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
    """会话级 token 统计。"""

    total: int = Field(description="总 token 数")
    by_model: dict[str, int] = Field(description="按模型拆分的 token 数")


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
    message_id: Optional[str] = Field(default=None, description="消息 ID（前端去重/归档去重），可缺省")
    timestamp: Optional[str] = Field(default=None, description="ISO 时间戳，可缺省")
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


class ServiceCapability(ContractModel):
    """单服务运行时能力（available=false 时 model 为 null）。"""

    available: bool = Field(description="后端连接可用")
    enabled: bool = Field(description="用户模式")
    model: Annotated[Optional[str], TsNull()] = Field(
        default=None, description="当前模型，unavailable 时为 null")
    default: str = Field(description="config.yaml 默认模型")


# ─────────────────────────── Server→Client ───────────────────────────


class SessionMessage(ContractModel):
    """会话历史回放（新连接建立时发送）。"""

    type: Literal["session"] = "session"
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


class ServerInfoMessage(ContractModel):
    """协议版本 + 服务器能力声明（仅 v2 客户端；取代 capabilities，避免重复帧）。

    `services` 复用 per-service 形状（available/enabled/model/default），
    Electron 设置面板与 v1 共用渲染逻辑。
    `connectionId`（批次 4）：桌面客户端连接 ID（Electron 附件上传 `X-Client-Connection`
    头的取值来源，BE-042 持有者校验闭环）；仅 v2 + electron 建连时下发，
    非 electron/普通 v2 恒省略（None → wire 缺省省略，v1 类型字节不变）。
    """

    type: Literal["server_info"] = "server_info"
    protocol_version: str = Field(default="2.0", description="协商的协议版本")
    server_version: str = Field(description="服务器版本（core/version.py）")
    features: ServerInfoFeatures = Field(description="能力特征（严禁超前声明）")
    services: dict[str, ServiceCapability] = Field(description="llm/stt/tts 三服务能力")
    connectionId: Optional[str] = Field(
        default=None, description="桌面客户端连接 ID（仅 v2 + electron 下发），可缺省")


# ─────────────────────────── 批次 2 v2 generation 事件 ───────────────────────────

# 分段通道 / 分段类型 / 完成原因 / ACK 状态——语义见 batch2 spec §3.2（kind 仅 text/markdown 实现，其余预留）


class UserAckMessage(ContractModel):
    """用户消息确认（v2）：接受后跟 generation.*，rejected 带 error 不生成。"""

    type: Literal["user_message.ack"] = "user_message.ack"
    messageId: str = Field(description="正式用户消息 ID（预分配，落库一致）")
    clientRequestId: str = Field(description="原客户端请求 ID")
    clientMessageId: Optional[str] = Field(default=None, description="客户端本地消息 ID，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，批次 2 恒 null，可缺省")
    receivedAt: str = Field(description="接收时间（UTC ISO）")
    status: Literal["accepted", "rejected"] = Field(description="accepted 进入生成；rejected 带 error")
    error: Optional[dict[str, Any]] = Field(default=None, description="rejected 时的 {code,message,retryable}，可缺省")


class GenerationStartedMessage(ContractModel):
    """生成开始（v2）。messageId = assistant 父消息 ID（generation 开始预分配）。"""

    type: Literal["generation.started"] = "generation.started"
    generationId: str = Field(description="生成实例 ID（gen_ 前缀）")
    messageId: str = Field(description="assistant 父消息 ID")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")
    requestId: Optional[str] = Field(default=None, description="回显 request_id（日志关联），可缺省")
    model: Optional[str] = Field(default=None, description="实际模型，可缺省")


class GenerationSegmentStartedMessage(ContractModel):
    """分段开始（v2）。"""

    type: Literal["generation.segment.started"] = "generation.segment.started"
    generationId: str = Field(description="生成实例 ID")
    segmentId: str = Field(description="分段 ID（seg_ 前缀）")
    segmentIndex: int = Field(description="全局分段序号（跨 inner_voice/public_reply 单调递增）")
    channel: Literal["inner_voice", "public_reply", "status"] = Field(description="分段通道")
    kind: Literal["text", "markdown", "image", "video", "audio", "file", "sticker", "link"] = Field(description="分段类型（P0 仅 text/markdown）")
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
    channel: Literal["inner_voice", "public_reply", "status"] = Field(description="分段通道")
    kind: Literal["text", "markdown", "image", "video", "audio", "file", "sticker", "link"] = Field(description="分段类型")
    content: Optional[str] = Field(default=None, description="完整段文本（客户端无需拼 delta 渲染），可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")


class GenerationCompletedMessage(ContractModel):
    """父完成（v2）——唯一终态（finishReason 只含 stop/tool_calls；取消/错误走 generation.failed）。"""

    type: Literal["generation.completed"] = "generation.completed"
    generationId: str = Field(description="生成实例 ID")
    messageId: str = Field(description="assistant 父消息 ID")
    channel: Literal["public_reply"] = Field(default="public_reply", description="父回复通道")
    finishReason: Literal["stop", "tool_calls"] = Field(description="完成原因（限 stop/tool_calls）")
    usage: TokenUsage = Field(description="回合 token 统计（turn_total=0/rounds=[] 表示无调用记录）")
    model: str = Field(description="实际模型")
    mood: Optional[str] = Field(default=None, description="心情（<mood> 标签落位），可缺省")
    completeness: Literal["complete"] = Field(default="complete", description="完整性（P0 恒 complete）")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")
    conversationUsage: Optional[ConversationUsage] = Field(
        default=None, description="会话级 token 累计（get_session_stats 填充），可缺省")


class GenerationFailedMessage(ContractModel):
    """父失败（v2）——取消/错误统一终态。code 用统一错误码目录。"""

    type: Literal["generation.failed"] = "generation.failed"
    generationId: str = Field(description="生成实例 ID")
    messageId: Optional[str] = Field(default=None, description="assistant 父消息 ID（已接受后恒非空），可缺省")
    code: str = Field(description="稳定错误码（GENERATION_PROVIDER_UNAVAILABLE/GENERATION_TIMEOUT/GENERATION_CANCELLED/INTERNAL 等）")
    message: str = Field(description="公开说明")
    retryable: bool = Field(description="是否可重试")
    details: dict[str, Any] = Field(default_factory=dict, description="公开安全详情，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")


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
    """表情/情绪变更。"""

    type: Literal["expression"] = "expression"
    expression: str = Field(description="表情/情绪标识")
    intensity: Optional[float] = Field(default=None, description="强度 0.0~1.0，可缺省")


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


class AudioEndMessage(ContractModel):
    """音频流结束标记。"""

    type: Literal["audio_end"] = "audio_end"
    stream_id: int = Field(description="音频流 ID")
    format: Optional[Literal["wav", "mp3"]] = Field(default="wav", description="音频格式，可缺省")


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
    params: dict[str, Any] = Field(default_factory=dict, description="动作参数")


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
    service: Literal["stt", "tts", "llm"] = Field(description="服务名")
    enabled: bool = Field(description="有效模式（永远随消息出现）")
    ok: bool = Field(description="操作是否成功")
    error: Annotated[Optional[str], TsNull()] = Field(default=None, description="错误，无则 null")


class HeartbeatAckMessage(ContractModel):
    """心跳应答。"""

    type: Literal["heartbeat_ack"] = "heartbeat_ack"


class ErrorMessage(ContractModel):
    """错误消息。"""

    type: Literal["error"] = "error"
    message: str = Field(description="错误描述")
    code: Optional[str] = Field(default=None, description="错误码（如 SESSION_NOT_FOUND），可缺省")


# ─────────────────────────── Client→Server ───────────────────────────


class TextMessage(ContractModel):
    """客户端发送文本/多模态消息。"""

    type: Literal["text"] = "text"
    content: str | list[ContentPart] = Field(description="纯文本或内容块列表")


class V2TextMessage(ContractModel):
    """v2 客户端文本消息（type=text + 幂等/定位字段；不进 ServerMessage 联合）。

    handler 按连接 protocol_version 路由：v2 连接解析追加字段，v1 忽略。
    非文本消息（content 数组）批次 2 沿用 v1 多模态路径。
    """

    type: Literal["text"] = "text"
    content: str = Field(description="纯文本")
    clientRequestId: str = Field(description="客户端请求 ID（幂等键，必填）")
    clientMessageId: Optional[str] = Field(default=None, description="客户端本地消息 ID，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，批次 2 忽略/恒 null，可缺省")


class InterruptMessage(ContractModel):
    """客户端打断指令。"""

    type: Literal["interrupt"] = "interrupt"


class SwitchModelMessage(ContractModel):
    """客户端请求切换模型。"""

    type: Literal["switch_model"] = "switch_model"
    service: str = Field(description="服务名（llm/stt/tts）")
    model: str = Field(description="目标模型标识")


class SetServiceMessage(ContractModel):
    """客户端请求设置服务开关（stt/tts 后端模式）。"""

    type: Literal["set_service"] = "set_service"
    service: Literal["stt", "tts"] = Field(description="服务名")
    enabled: bool = Field(description="是否启用")


class ToolApprovalMessage(ContractModel):
    """客户端提交工具审批结果。"""

    type: Literal["tool_approval"] = "tool_approval"
    request_id: str = Field(description="审批请求 ID")
    approved: bool = Field(description="是否批准")


class WakeToggleMessage(ContractModel):
    """客户端切换唤醒词开关。"""

    type: Literal["wake_toggle"] = "wake_toggle"
    enabled: bool = Field(description="是否启用")


class ActivateSessionMessage(ContractModel):
    """客户端激活/切换会话（v2）：服务端切 active 并重发新活动会话回放。

    未知 conversationId → 不回放，发 ErrorMessage{code: SESSION_NOT_FOUND}（批次 1 错误信封）。
    """

    type: Literal["activate_session"] = "activate_session"
    conversationId: str = Field(description="目标会话 ID（必填）")


# ─────────────────────────── 批次 4 Desktop 工具桥（BE-040/041/043） ───────────────────────────


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

    bridge._deliver 定位 `client_platform == "electron"` 的 v2 连接 → _send_json 本帧；
    v1 连接不收 desktop 帧（D-l）。paramHash = 规范化 arguments 的 SHA-256（BE-043
    篡改拒绝权威依据）；expiresAt = 桥 request_timeout 的绝对过期时间（UTC ISO）。
    """

    type: Literal["tool.request"] = "tool.request"
    callId: str = Field(description="调用 ID（call_ 前缀）")
    toolName: str = Field(description="工具名（desktop.file.read 等）")
    toolVersion: str = Field(description="工具版本（语义化版本）")
    runtime: str = Field(description="工具 Runtime（desktop）")
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


# ─────────────────── ServerMessage 判别联合（模块级） ───────────────────

ServerMessage = Annotated[Union[
    SessionMessage,
    CapabilitiesMessage,
    ServerInfoMessage,
    DeltaMessage,
    MessageMessage,
    TokenUsageMessage,
    ExpressionMessage,
    StateMessage,
    AudioMessage,
    AudioEndMessage,
    TypingMessage,
    TranscriptionMessage,
    ActionMessage,
    CommandMessage,
    WakeMessage,
    SwitchModelResultMessage,
    ServiceModeResultMessage,
    HeartbeatAckMessage,
    ErrorMessage,
    UserAckMessage,
    GenerationStartedMessage,
    GenerationSegmentStartedMessage,
    GenerationDeltaMessage,
    GenerationSegmentCompletedMessage,
    GenerationCompletedMessage,
    GenerationFailedMessage,
    ToolRequestMessage,
], Field(discriminator="type")]
