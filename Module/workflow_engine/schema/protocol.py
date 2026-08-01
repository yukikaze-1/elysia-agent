"""第一层：跨层协议 —— CognitiveLoop ↔ TaskSystem 输入/输出契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .types import (
    AgentId,
    ApprovalId,
    IsoTimestamp,
    NodeId,
    RBACLevel,
    SessionId,
    SubmitRejectionCode,
    TaskErrorCode,
    ResumeErrorCode,
    NodeErrorCode,
    TaskId,
    TaskStatus,
    now_iso,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 第一层：跨层协议 —— CognitiveLoop ↔ TaskSystem 输入/输出
# ═══════════════════════════════════════════════════════════════════════════════

# ── 约束 ──────────────────────────────────────────────────────────────────────

class ConstraintConfig(BaseModel):
    """全局约束配置（TaskSystem 构造时注入）。

    TaskSystem 内所有执行路径强制遵守。这些是"天花板"——
    通过 TaskConstraints.resolve(global) 合并，任务级 None → 全局值，超限 → clamp。

    字段：
        max_total_tokens:     整个任务 LLM 调用总 token 上限（硬）
        max_context_tokens:   单次 prompt 最大 token 数（硬）
        max_failure_count:    连续失败 N 次后熔断（硬）。
                              当前 v1 中 replan 只能由 failure 触发，故此值
                              同时充当 max_replans。若以后引入非失败驱动的 replan
                              （如"换个更优方案"），需拆为独立字段。
        max_dag_nodes:        DAG 节点数上限（硬）
        max_retries_per_node: 单节点最大重试次数（硬）
        max_mutations:        敏感操作次数上限（硬）
        max_task_duration_s:  整个任务最大执行时间（秒）（硬）
        max_tool_error_chars: 工具错误消息截断长度（软）
    """
    max_total_tokens: int       = Field(default=100_000,  ge=1)   # 至少 1 token，否则无法执行
    max_context_tokens: int     = Field(default=32_000,   ge=1)   # 至少 1 token，否则无法投 prompt
    max_failure_count: int      = Field(default=5,         ge=1)
    max_dag_nodes: int          = Field(default=20,        ge=1)
    max_retries_per_node: int   = Field(default=3,         ge=0)
    max_mutations: int          = Field(default=10,        ge=0)
    max_task_duration_s: float  = Field(default=600.0,     ge=0)  # 10 分钟
    max_tool_error_chars: int   = Field(default=2000,      ge=0)


class TaskConstraints(BaseModel):
    """任务级约束（DelegationPreparer 产出）。

    所有字段 None 表示"不覆盖，使用全局默认"。
    调用 resolve(global) 后得到所有字段均已填充的有效约束，
    Planner / PlanValidator 直接使用，不再手写 min/fallback。

    字段：
        max_dag_nodes:       本次任务 DAG 步数上限（LLM 根据意图复杂度估算）
        max_mutations:       本次任务敏感操作次数上限（LLM 根据意图风险等级估算）
        max_total_tokens:    本次任务总 token 上限（None=全局默认，LLM 估算或调用方指定）
        max_task_duration_s: 本次任务最大时长秒数（None=全局默认）
        max_failure_count:   本次任务最大连续失败次数（None=全局默认）
    """
    max_dag_nodes: int | None = Field(default=None, ge=1)
    max_mutations: int | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_task_duration_s: float | None = Field(default=None, ge=0)
    max_failure_count: int | None = Field(default=None, ge=1)

    def resolve(self, global_: ConstraintConfig) -> TaskConstraints:
        """用全局约束填充所有 None 字段，返回全量有效约束。

        规则：
            1. None → 取全局值
            2. 非 None 但超过全局 → 以全局为准（clamp），记录 warning
            3. 非 None 且 ≤ 全局 → 保留任务级值

        任务级约束是全局的"子集天花板"——只应更严格，不应更宽松。
        DelegationPreparer 产出超过全局的值是 bug，但不会导致崩溃。

        Args:
            global_: 全局约束配置（天花板）。

        Returns:
            TaskConstraints: 全量有效约束（所有字段已填充，不超过全局上限）。
        """
        import logging
        _log = logging.getLogger(__name__)

        def _resolve_field(name: str, task_val, global_val, clamp_reason: str = ""):
            """解析单个字段：None → global；> global → clamp+warn；≤ global → task。"""
            if task_val is None:
                return global_val
            if task_val > global_val:
                _log.warning(
                    "TaskConstraints.resolve: %s=%s 超过全局上限 %s，clamp 到全局值（%s）",
                    name, task_val, global_val, clamp_reason,
                )
                return global_val
            return task_val

        return TaskConstraints(
            max_dag_nodes=_resolve_field(
                "max_dag_nodes", self.max_dag_nodes, global_.max_dag_nodes,
                "DAG 节点数",
            ),
            max_mutations=_resolve_field(
                "max_mutations", self.max_mutations, global_.max_mutations,
                "敏感操作次数",
            ),
            max_total_tokens=_resolve_field(
                "max_total_tokens", self.max_total_tokens, global_.max_total_tokens,
                "总 token",
            ),
            max_task_duration_s=_resolve_field(
                "max_task_duration_s", self.max_task_duration_s, global_.max_task_duration_s,
                "任务时长",
            ),
            max_failure_count=_resolve_field(
                "max_failure_count", self.max_failure_count, global_.max_failure_count,
                "连续失败次数",
            ),
        )


# ── CognitiveLoop 侧类型（TaskSystem 只引用，不拥有）─────────────────────────────

# Tool 已升级为正式描述符模型（对齐 Module/tool.ToolPlugin 的描述层字段）。
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


class Tool(BaseModel):
    """工具描述符——TaskSystem 对 Module/tool 工具的轻量投影。

    只持有 TaskSystem 层需要的字段：
        name:         工具唯一标识符（与 ToolPlugin.name 对应，Executor 据此过滤 definitions）
        description:  工具功能描述（Planner 注入 prompt 供 LLM 选工具）
        capabilities: 工具能力标签（对齐 Module/tool.Capability，v2 RBAC 校验用）

    注意：参数 schema 不在此重复——Executor 运行时从 ToolManager.definitions() 获取
    完整的 OpenAI function-calling 格式 schema，此模型仅作为"许可列表"传递。
    不含执行逻辑，不含 SafetyProfile——这些由 ToolManager 内部持有。
    """
    name: str
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)


# ── 委派上下文 ──────────────────────────────────────────────────────────────────

class ReturnMode(StrEnum):
    """Planner / Evaluator 产出报告模式。"""
    NATURAL_LANGUAGE = "natural_language"  # 自然语言总结（默认）
    STRUCTURED = "structured"              # 结构化数据（node_results）


class DelegationContext(BaseModel):
    """DelegationPreparer 产出的委派沙盒。

    Planner 在这个沙盒内工作——只能看到 DelegationPreparer 筛选过的工具和上下文。

    所有字段由 DelegationPreparer.prepare() 填充，
    传入 TaskSystem.submit() 后由 Planner 直接消费。

    字段：
        intent:              LLM 输出的完整任务意图（非用户原始输入）
        hints:               LLM 观察到的元信息（紧急程度、偏好等），可为 None
        tools:              本次任务可用的工具列表（从全量工具中筛选）
        task_constraints:    本次任务级限制（None 字段 = 不覆盖全局约束）
        summarized_context:  LLM 摘要的相关上下文（≤ 500 字）
        session_id:          关联会话 ID，用于日志和 Debug 追踪
        return_mode:         产出报告模式——自然语言总结 或 结构化数据
    """
    intent: str
    hints: str | None = None
    tools: list[Tool] = Field(default_factory=list)
    task_constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    summarized_context: str = ""
    session_id: SessionId
    return_mode: ReturnMode = ReturnMode.NATURAL_LANGUAGE


class DelegationStatus(StrEnum):
    """DelegationPreparer.prepare() 的三态判定结果。"""
    OK = "ok"                              # 委派上下文准备完成，可 submit
    NO_MATCHING_TOOLS = "no_matching_tools"  # 工具库缺所需工具
    # TODO: v1 不区分"零工具匹配"和"部分工具缺失"，两者都返回 NO_MATCHING_TOOLS。
    #       后续可拆为 NO_TOOLS_AVAILABLE（全缺）和 PARTIAL_TOOLS_MISSING（部分缺）。
    TOO_VAGUE = "too_vague"                # 意图模糊，需用户澄清


class DelegationResult(BaseModel):
    """DelegationPreparer.prepare() 的产出。

    status 决定 context 是否有值：
        ok               → context 有值，调用 TaskSystem.submit()
        no_matching_tools → context=None，工具库缺所需工具
        too_vague        → context=None，意图模糊需用户澄清

    字段：
        status:              三态判定结果
        context:             status=ok 时的委派上下文
        reason:              LLM 判定理由（始终有，用于调试）
        missing_tools:      status=no_matching_tools 时，缺少的工具列表
        suggestion:          status=too_vague 时，给用户的澄清建议
    """
    status: DelegationStatus
    context: DelegationContext | None = None
    reason: str = ""
    missing_tools: list[str] = Field(default_factory=list)
    suggestion: str | None = None

    token_usage: dict | None = None
    # LLM 调用消耗: {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
    # status=ok 时始终有值；异常时可能为 None

    # UserProfile 字段由 CognitiveLoop 侧定义，当前占位。
    # session_messages 截断策略后续讨论。


# ── 提交结果 ────────────────────────────────────────────────────────────────────

class SubmitError(BaseModel):
    """submit() 拒绝时的结构化错误。

    字段：
        code:        错误码
        message:     人类可读的错误描述
        details:     额外上下文（如超限值 vs 限制值）
        suggestions: 修复建议列表
    """
    code: SubmitRejectionCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)


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


# ── HITL 审批数据 ──────────────────────────────────────────────────────────────

class HITLData(BaseModel):
    """HITL 审批请求数据（TaskSystem → HITLNotifier.request_approval）。

    同一份数据既走 HITLNotifier 快通道（强类型），
    也走 TaskMailbox 审计流（TaskEvent.data.approval_needed）。

    字段：
        task_id:       所属任务 ID
        approval_id:   审批请求 ID（取消时用于匹配前端卡片）
        agent_id:      发起审批的 Worker ID（回传时路由用）
        node_id:       当前 DAG 节点 ID
        node_task:     当前节点在做什么，来自 DAGNode.description
                       （例："搜索昨天天气数据"），帮助用户理解 MUTATION 的上下文
        tool_name:     即将执行的工具名（例："send_email"）
        params:        工具参数预览（脱敏后）
        rbac_level:    工具权限级别，前端据此渲染审批卡片危险程度
        risk_summary:  风险摘要（操作类型、影响范围、参数预览）
    """
    task_id: TaskId
    approval_id: ApprovalId
    agent_id: AgentId
    node_id: NodeId
    node_task: str                       # 当前节点的目标任务描述
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    rbac_level: RBACLevel = RBACLevel.MUTATION
    risk_summary: str = ""
