"""WS 消息契约：session 域（ARCH-INC-P2-02 物理拆分）。

装配点见 `frontend_contract/ws_message/__init__.py`；该文件是唯一的
`ServerMessage`/`ClientMessage` 联合定义位置。
"""
from __future__ import annotations


from frontend_contract.base import ContractModel, TsNull
from frontend_contract.version import PROTOCOL_VERSION
from pydantic import ConfigDict, Discriminator, Field, model_validator
from typing import Annotated, Any, Literal, Optional, Union
from frontend_contract.ws_message._types import ServerInfoFeatures, ServiceCapability, ServiceName, SessionMessageItem, TokenStats



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



class SwitchModelMessage(ContractModel):
    """客户端请求切换模型。"""

    type: Literal["switch_model"] = "switch_model"
    service: str = Field(description="服务名（llm/stt/tts）")
    model: str = Field(description="目标模型标识")
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
