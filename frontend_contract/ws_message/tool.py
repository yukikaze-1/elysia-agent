"""WS 消息契约：tool 域（ARCH-INC-P2-02 物理拆分）。

装配点见 `frontend_contract/ws_message/__init__.py`；该文件是唯一的
`ServerMessage`/`ClientMessage` 联合定义位置。
"""
from __future__ import annotations


from frontend_contract.base import ContractModel, TsNull
from pydantic import ConfigDict, Discriminator, Field, model_validator
from typing import Annotated, Any, Literal, Optional, Union
from frontend_contract.ws_message._types import ToolAttachment, ToolRuntime, ToolStatusEnum



class ToolApprovalMessage(ContractModel):
    """客户端提交工具审批结果。"""

    type: Literal["tool_approval"] = "tool_approval"
    request_id: str = Field(description="审批请求 ID")
    approved: bool = Field(description="是否批准")
    clientRequestId: Optional[str] = Field(
        default=None, description="客户端请求 ID（限流关联回显），可缺省")



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
    inputSummary: Optional[str] = Field(default=None, description="服务端脱敏参数摘要（summarize_tool_arguments），可缺省")
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
