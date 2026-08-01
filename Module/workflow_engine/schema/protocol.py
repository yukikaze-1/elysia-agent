"""第一层：跨层协议 —— 剩余本地类型。

跨边类型（DelegationContext/DelegationResult/DelegationStatus/SubmitError/HITLData/
ConstraintConfig/WorkflowConstraints/ReturnMode）与 Tool 已随契约层提取迁出：
  - 跨边类型 → Module/workflow_contract/schema.py
  - Tool     → Module/tool/tool_descriptor.py
本文件仅保留引擎侧本地类型：UserProfile（CognitiveLoop 侧类型，占位）、ResumeError。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .types import ResumeErrorCode


# ═══════════════════════════════════════════════════════════════════════════════
# CognitiveLoop 侧类型（WorkflowEngine 只引用，不拥有）
# ═══════════════════════════════════════════════════════════════════════════════

# UserProfile 仍为占位——字段由 CognitiveLoop 侧定义。

class UserProfile(BaseModel):
    """用户画像（CognitiveLoop 管理）。

    DelegationPreparer 读取此信息来理解用户偏好和限制，
    但不修改——只读消费。

    TODO: 字段由 CognitiveLoop 侧定义。当前占位，防止裸 dict。
    """
    model_config = {"extra": "allow"}  # 暂时允许任意字段

    # 占位字段——供集成测试使用，后续由 CognitiveLoop 填充
    preferences: str | dict[str, str] = ""
    language: str = "zh"
    display_name: str = ""  # 用户显示名——来自 config.agent.user_name


# ═══════════════════════════════════════════════════════════════════════════════
# 提交结果
# ═══════════════════════════════════════════════════════════════════════════════

class ResumeError(BaseModel):
    """resume() 失败时的结构化错误。

    调用方（CognitiveLoop）根据 code 决定回复策略：
        CHECKPOINT_NOT_FOUND / CORRUPTED / VERSION_MISMATCH
            → 告知用户"任务记录丢失/不兼容，无法恢复"，建议重新提交
        INVALID_STATE
            → 内部错误，告知用户"恢复失败"，建议重试或重新提交
        PENDING_APPROVALS_FAILED
            → 告知用户"审批恢复失败"，建议检查前端收件箱或重新提交
        NODE_RECOVERY_FAILED
            → 告知用户"部分已完成步骤丢失"，建议重新提交
    """
    code: ResumeErrorCode
    message: str                            # 人类可读的错误描述
    details: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)  # 修复建议
