"""WS 消息契约建模：Server→Client 18 具名成员 + Client→Server 消息 + 支持类型（零依赖）。

对照 `frontend/src/types/protocol.ts` 逐字段对齐；ts_null vs X? 按发送点实际 wire 定性：
  - switch_model_result.error：`**result` 展开，成功无键 → Optional[str]（X?）
  - service_mode_result.error：`result.get("error")` 恒写 → Annotated[Optional[str], TsNull()]
  - CapabilitiesMessage.services.{stt,tts,llm}.model：available=false 时显式 null → TsNull
  - 其余 Optional 字段（stream_id/error?/format? 等）默认 X?（缺省省略）
判别联合别名 `ServerMessage` 模块级可访问（__main__ 经 get_args 收集成员）。
"""
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import Field

from frontend_contract.base import ContractModel, TsNull

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


ContentPart = Annotated[Union[TextPart, ImagePart], Field(discriminator="type")]


class ToolCallFunction(ContractModel):
    """工具函数名 + 参数。"""

    name: str = Field(description="函数名")
    arguments: str = Field(description="JSON 字符串参数（不解析）")


class ToolCallData(ContractModel):
    """工具调用数据。"""

    id: str = Field(description="工具调用 ID")
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


# ─────────────────────────── Client→Server ───────────────────────────


class TextMessage(ContractModel):
    """客户端发送文本/多模态消息。"""

    type: Literal["text"] = "text"
    content: str | list[ContentPart] = Field(description="纯文本或内容块列表")


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


# ─────────────────── ServerMessage 判别联合（模块级） ───────────────────

ServerMessage = Annotated[Union[
    SessionMessage,
    CapabilitiesMessage,
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
], Field(discriminator="type")]
