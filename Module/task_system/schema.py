"""TaskSystem 核心数据结构。

分层：
  第一层 跨层协议 —— CognitiveLoop ↔ TaskSystem 的输入/输出契约
  第二层 核心输出 —— TaskSystem → Agent 的事件流 + 查询视图
  第三层 内部编排 —— Planner / Executor / Evaluator 的内部数据模型（待定）

所有类型使用 Pydantic BaseModel，在 TaskSystem 内部保持类型安全。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal, NewType

from pydantic import BaseModel, Field


def now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。

    TaskSystem 内所有 timestamp 字段统一使用此函数生成，
    替代各模块内重复的 _now() / datetime.now(timezone.utc).isoformat() 样板代码。
    """
    return datetime.now(timezone.utc).isoformat()


# ── 基础类型 ────────────────────────────────────────────────────────────────────

TaskId = NewType("TaskId", str)
"""任务唯一标识符。

NewType 而非 str：防止调用方随意传参（如把 user_input 当 task_id）。
TaskSystem 内部生成，对外透明。序列化就是 str，零开销。
"""

AgentId = NewType("AgentId", str)
"""Agent 唯一标识符（Sub-Agent / Worker Agent）。

TaskSystem 内部生成，用于 submit_approval 路由到等待审批的 Worker。
"""

ApprovalId = NewType("ApprovalId", str)
"""审批请求唯一标识符。

TaskSystem 内部生成，用于 cancel_approval 清理前端收件箱卡片。
"""

SessionId = NewType("SessionId", str)
"""会话唯一标识符。

由 CognitiveLoop 管理，TaskSystem 仅用于日志关联和 Debug 追踪。
NewType 防止调用方把 task_id / user_input 误当 session_id 传入。
"""

NodeId = NewType("NodeId", str)
"""DAG / DBT 节点唯一标识符。

Planner 生成 DAGNode 时分配，Executor 生成 DBTNode 时沿用或派生。
NewType 防止调用方把 task_id / artifact_id 误当 node_id 传入。
"""

ArtifactId = NewType("ArtifactId", str)
"""产物唯一标识符。

ArtifactStore 存储时的 key，与 DAGNode.output_as 对应。
NewType 防止调用方把 node_id / task_id 误当 artifact_id 传入。
"""


# ═══════════════════════════════════════════════════════════════════════════════
# 基础枚举
# ═══════════════════════════════════════════════════════════════════════════════

class TaskStatus(StrEnum):
    """任务状态机——11 态。

    入口阶段
        PENDING → PLANNING → RUNNING

    执行中分支（从 RUNNING 出发）
        软暂停:   RUNNING → PAUSE_REQUESTED → PAUSED → RESUMING → RUNNING
        取消暂停: PAUSED → CANCELLED（直接取消，无需 resume）
        审批:     RUNNING → WAITING_APPROVAL → RUNNING
                  WAITING_APPROVAL → CANCELLED（等待审批时取消）
        软取消:   RUNNING → CANCEL_REQUESTED → CANCELLED
        完成:     RUNNING → COMPLETED

    早期退出
        PENDING → CANCELLED（submit 后立即取消）
        PLANNING → CANCELLED（规划阶段取消）
        PLANNING → FAILED（规划阶段异常）

    终态: COMPLETED / FAILED / CANCELLED（不可逆）
    内部: REPLAN 在 RUNNING 内部完成（Planner.replan → apply_patch → 继续 RUNNING），不暴露为独立状态。
    """
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    RESUMING = "resuming"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


class SubmitRejectionCode(StrEnum):
    """submit() 同步拒绝码——5 项。

    TaskSystem.submit() 参数校验不通过时返回，
    CognitiveLoop 据此生成用户可见的错误信息。
    不会出现在 TaskState 或 TaskEvent 中。
    """
    NOT_READY = "not_ready"                              # TaskSystem 尚未 start()
    INVALID_PARAM = "invalid_param"                     # 参数校验不通过
    PLAN_TOO_LARGE = "plan_too_large"                   # DAG 预估节点数超限
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"  # 上下文窗口不足
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"     # 预估 token 消耗超用户剩余配额


class TaskErrorCode(StrEnum):
    """任务运行时错误码——10 项。

    TaskState.error_code 使用，checkpoint 持久化，
    TaskEvent(type="failed") 的 error_type 字段。
    调用方（CognitiveLoop）据此生成用户可见的错误信息。

    按发生阶段分组：
        规划阶段:    PLANNING_FAILED, PLAN_TOO_LARGE
        执行阶段:    TOOL_ERROR, PERMISSION_DENIED, TOKEN_BUDGET_EXCEEDED
        资源耗尽:    FAILURE_MELTDOWN, TIMEOUT, MUTATION_LIMIT_REACHED
        外部:        CANCELLED_BY_USER
        兜底:        INTERNAL_ERROR
    """
    PLANNING_FAILED = "planning_failed"                # Planner LLM 调用失败，无法生成 Plan
    PLAN_TOO_LARGE = "plan_too_large"                  # Planner 生成的 DAG 节点数超限
    TOOL_ERROR = "tool_error"                          # 工具执行错误（任务级）
    PERMISSION_DENIED = "permission_denied"            # 审批被拒（任务级）
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"    # 任务总 token 消耗达上限
    FAILURE_MELTDOWN = "failure_meltdown"              # 失败次数达上限，禁止 Replan
    TIMEOUT = "timeout"                                 # 任务整体超时
    MUTATION_LIMIT_REACHED = "mutation_limit_reached"  # 敏感操作次数达上限
    CANCELLED_BY_USER = "cancelled_by_user"            # 用户主动取消
    INTERNAL_ERROR = "internal_error"                  # 内部异常（unexpected 兜底）


class ResumeErrorCode(StrEnum):
    """resume() 失败时的错误码。

    调用方（CognitiveLoop）据此生成用户可见的错误信息。
    """
    CHECKPOINT_NOT_FOUND = "checkpoint_not_found"            # checkpoint 文件不存在
    CHECKPOINT_CORRUPTED = "checkpoint_corrupted"            # checkpoint 数据损坏
    CHECKPOINT_VERSION_MISMATCH = "checkpoint_version_mismatch"  # schema 版本不兼容
    INVALID_STATE = "invalid_state"                          # 非 PAUSED 状态 / CAS 冲突
    PENDING_APPROVALS_FAILED = "pending_approvals_failed"    # 重推审批请求失败
    NODE_RECOVERY_FAILED = "node_recovery_failed"            # 已完成节点恢复失败


class NodeStatus(StrEnum):
    """DAG 节点的运行时状态——8 态。

    Orchestrator 主循环中 NodeState 使用，DAGNodeInfo（查询视图）复用。

    正常流程
        PENDING → RUNNING → SUCCESS

    重试
        RUNNING → FAILED → PENDING → RUNNING（达到上限 → UNRECOVERABLE）

    审批阻断
        RUNNING → WAITING_APPROVAL → RUNNING（审批通过）
        WAITING_APPROVAL → FAILED（审批拒绝）

    任务取消（批量标记）
        PENDING / RUNNING / WAITING_APPROVAL → CANCELLED

    跳过
        PENDING → SKIPPED（可选节点在 replan 后被移除）
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNRECOVERABLE = "unrecoverable"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLED = "cancelled"


class RBACLevel(StrEnum):
    """工具权限级别。注册时声明，运行时校验。

    TODO: 归属待定。Tools 在 Module/tool/ 声明权限级别，TaskSystem 执行时校验。
    两个模块都需要此枚举。后续确定 canonical home（建议 Module/tool/），
    TaskSystem 侧改为 re-export。
    """
    READ_ONLY = "read_only"       # 只读，完全自主
    SAFE_WRITE = "safe_write"     # 安全写入（沙箱内），条件自主
    MUTATION = "mutation"         # 外部变更，必须审批


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


# ═══════════════════════════════════════════════════════════════════════════════
# 第二层：核心输出 —— TaskMailbox 事件 + 查询视图
# ═══════════════════════════════════════════════════════════════════════════════

# ── TaskMailbox 事件 ────────────────────────────────────────────────────────────

class TaskEventType(StrEnum):
    """TaskMailbox 事件类型——6 种。"""
    PROGRESS = "progress"
    APPROVAL_NEEDED = "approval_needed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class TaskEvent(BaseModel):
    """TaskMailbox 中传输的事件。

    type 决定 data 的 shape（注释为参考 shape，不强制校验）：
        progress:         进度更新（来自 Orchestrator）
        approval_needed:  审批请求审计流副本（快通道由 HITLNotifier 推送）
        completed:        任务成功完成
        failed:           任务失败
        cancelled:        任务被取消
        paused:           任务已暂停并保存 checkpoint

    字段：
        type: 事件类型
        data: 事件负载，shape 随 type 变化
    """
    type: TaskEventType
    data: dict[str, Any] = Field(default_factory=dict)

    # ── data shape 参考 ──────────────────────────────────────────────────────

    # progress: {
    #     "task_id": TaskId,
    #     "node_id": NodeId,
    #     "node_description": str,  # 当前节点在做什么
    #     "percent": float,         # 0~1，整个任务的完成比例
    #     "message": str,           # 简短描述
    # }

    # approval_needed: {            # 与 HITLData 字段一一对应
    #     "task_id": TaskId,
    #     "approval_id": ApprovalId,
    #     "agent_id": AgentId,
    #     "node_id": NodeId,
    #     "node_task": str,
    #     "tool_name": str,
    #     "params": dict[str, Any],
    #     "rbac_level": str,
    #     "risk_summary": str,
    # }

    # completed: {
    #     "task_id": TaskId,
    #     "result": str,            # 自然语言摘要
    #     "summary": str,           # 简短摘要
    #     "total_nodes": int,
    #     "completed_nodes": int,
    #     "failed_nodes": int,
    # }

    # failed: {
    #     "task_id": TaskId,
    #     "error": str,             # 人类可读错误描述
    #     "error_type": TaskErrorCode,  # 任务级错误码
    # }

    # cancelled: {
    #     "task_id": TaskId,
    #     "reason": str,            # 取消原因
    # }

    # paused: {
    #     "task_id": TaskId,
    #     "message": str,           # 暂停说明
    # }


# ── 查询视图：任务摘要 ──────────────────────────────────────────────────────────

class TaskSummary(BaseModel):
    """任务完成后/查询时的摘要视图。

    Debug WebUI 和用户查询（"刚才那个任务怎么样了？"）使用。
    字段偏展示——tools_used 用字符串名，errors 用扁平字符串列表
    而非结构化 error+error_code。
    """
    task_id: TaskId
    status: TaskStatus
    intent: str
    created_at: str = ""                  # ISO 8601，对齐 TaskState.created_at
    duration_ms: int = 0                  # 总耗时（查询视图预计算，方便直接展示）
    nodes: dict[str, int] = Field(        # {"total": N, "completed": N, "failed": N, "skipped": N}
        default_factory=lambda: {"total": 0, "completed": 0, "failed": 0, "skipped": 0}
    )
    tools_used: list[str] = Field(default_factory=list)   # 审计：本任务调用了哪些工具名
    token_usage: dict[str, int] = Field(  # {"planner": N, "executor": N, "evaluator": N, ...}
        default_factory=dict
    )
    approvals: dict[str, int] = Field(    # {"requested": N, "approved": N, "denied": N}
        default_factory=lambda: {"requested": 0, "approved": 0, "denied": 0}
    )
    error: str | None = None                    # 任务级错误消息（来自 TaskState.error，对齐 TaskResult）
    error_code: TaskErrorCode | None = None     # 任务级错误码（来自 TaskState.error_code）
    # 节点级诊断见 TaskResult.node_results
    replans: int = 0


# ── 查询视图：进度 ──────────────────────────────────────────────────────────────

class TaskProgress(BaseModel):
    """单任务执行进度快照。"""
    task_id: TaskId
    status: TaskStatus = TaskStatus.PENDING
    current_node_id: NodeId | None = None       # 当前正在执行的节点
    completed_nodes: int = 0                    # 已完成节点数
    total_nodes: int = 0                        # 总节点数
    elapsed_ms: int = 0                         # 已耗时
    estimated_remaining_ms: int | None = None   # 预估剩余时间（None=无法估算）


# ── 查询视图：DAG ───────────────────────────────────────────────────────────────

class DAGNodeInfo(BaseModel):
    """DAG 图中单个节点的展示信息（查询用，非内部编排数据）。

    注意：当前不包含 tool_name——v1 工具由 Executor 运行时匹配，
    整个管线（NodeResult → NodeState → DAGNodeInfo）尚未携带工具名。
    后续在 NodeResult 上补 tool_name 时同步加回。
    """
    node_id: NodeId
    description: str = ""
    status: NodeStatus = NodeStatus.PENDING
    progress: float = 0.0
    error: str | None = None            # 人类可读，来自 NodeState.error
    error_code: NodeErrorCode | None = None  # 机器可读，来自 NodeState.error_code


class TaskDAG(BaseModel):
    """任务 DAG 的可视化数据（Debug WebUI 渲染用）。"""
    nodes: list[DAGNodeInfo] = Field(default_factory=list)
    edges: list[tuple[NodeId, NodeId]] = Field(default_factory=list)


# ── 查询视图：Agent 池状态 ──────────────────────────────────────────────────────

class AgentType(StrEnum):
    """Agent 类型。"""
    SUB = "sub"        # Sub-Agent：任务级执行单元
    WORKER = "worker"  # Worker Agent：节点级执行单元


class AgentStatus(StrEnum):
    """Agent 运行时状态——TaskStatus 的子集（只含 Agent 可能的状态）。"""
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_APPROVAL = "awaiting_approval"


class AgentInfo(BaseModel):
    """单个 Agent 的运行信息。"""
    agent_id: AgentId
    agent_type: AgentType
    task_id: TaskId
    node_id: NodeId | None = None          # Worker 专用
    status: AgentStatus = AgentStatus.RUNNING
    started_at: str = ""                   # ISO 8601
    last_seen_at: str = ""                 # ISO 8601
    duration_ms: int = 0


class AgentList(BaseModel):
    """Agent 池当前状态。"""
    sub_agents: list[AgentInfo] = Field(default_factory=list)
    worker_agents: list[AgentInfo] = Field(default_factory=list)
    total_capacity: int = 0
    available_slots: int = 0


# ── 查询视图：Token 统计 ────────────────────────────────────────────────────────

class TokenComponentType(StrEnum):
    """Token 消耗来源组件。"""
    PLANNER = "planner"
    EXECUTOR = "executor"
    EVALUATOR = "evaluator"
    REFLECTOR = "reflector"    # TODO: 尚未实现，Token 统计预留
    SCHEDULER = "scheduler"    # TODO: v2 调度器
    SYNTHESIZER = "synthesizer"  # TODO: v2 结果合成器
    REPLANNER = "replanner"    # TODO: v2 独立 Replanner（与 Planner LLM 调用不同）
    REDUCER = "reducer"        # TODO: v2 结果归约器


class TokenPeriod(StrEnum):
    """Token 统计时间段。"""
    TODAY = "today"
    WEEK = "week"
    MONTH = "month"
    ALL = "all"


class TokenComponent(BaseModel):
    """单个组件的 token 消耗记录。"""
    component: TokenComponentType
    node_id: NodeId | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0       # prompt + completion（查询视图预计算）
    model: str = ""


class TokenUsage(BaseModel):
    """单个任务的 token 消耗明细。"""
    task_id: TaskId
    components: list[TokenComponent] = Field(default_factory=list)


class TokenStats(BaseModel):
    """聚合 token 统计（按时间段）。"""
    period: TokenPeriod = TokenPeriod.TODAY
    total_tokens: int = 0
    by_component: dict[str, int] = Field(default_factory=dict)  # TokenComponentType → 总量
    by_model: dict[str, int] = Field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════════
# 第三层：内部编排 —— Planner / Executor / Evaluator 数据模型
# ═══════════════════════════════════════════════════════════════════════════════
# ↓ 下面这些字段待敲定，先留骨架 ↓


# ── Orchestrator 运行状态 ─────────────────────────────────────────────────────

class OrchestratorRunStatus(StrEnum):
    """Orchestrator.run() 的返回状态——5 态。

    Orchestrator 驱动 Plan 直到完成、遇到障碍、或被外部中断。
    非 SUCCESS 状态由 Sub-Agent 做决策。

    内部出向
        SUCCESS       — 所有 required 节点完成
        FAILED        — 不可恢复（资源耗尽、内部异常）
        NEEDS_REPLAN  — 结构性失败，DAG 走不通，需要修改 Plan
        BLOCKED       — 等待用户审批回传
    外部中断
        PAUSED        — 收到暂停请求，已保存 checkpoint，可恢复
    """
    SUCCESS = "success"
    FAILED = "failed"
    NEEDS_REPLAN = "needs_replan"
    BLOCKED = "blocked"
    PAUSED = "paused"


class OrchestratorResult(BaseModel):
    """Orchestrator.run() 的返回值。

    SUCCESS 时 checkpoint_state 有最终快照。
    NEEDS_REPLAN 时 error_context 包含失败节点和诊断信息。
    BLOCKED 时等待外部审批回传。
    PAUSED 时保存了 checkpoint，TaskSystem 可调用 resume() 恢复。
    """
    status: OrchestratorRunStatus
    error_context: dict[str, Any] = Field(default_factory=dict)
    # ↑ NEEDS_REPLAN / FAILED 时包含:
    #     {"reason": str, "failed_nodes": list[NodeId]}
    #     特殊情况（死锁、内部异常）只有 reason，无 failed_nodes
    checkpoint_state: TaskCheckpoint | None = None
    # ↑ 当前 TaskState + ExecutionState 快照。Sub-Agent 用于:
    #     PAUSED       → CheckpointManager.save(checkpoint_state)，等 resume 后恢复
    #     BLOCKED      → 等审批回传后恢复执行
    #     NEEDS_REPLAN → checkpoint_state.exec.node_states 传给 Planner.replan()
    #     死锁 / ABORT 时为 None（快照无意义）


# ── Orchestrator 状态追踪 ─────────────────────────────────────────────────────────

class NodeState(BaseModel):
    """Orchestrator 内部：单个节点的运行时状态。

    与 DAGNode（声明式）和 NodeResult/NodeResultEvaluation（执行/评估产出）不同——
    NodeState 是 Orchestrator 主循环中追踪"这个节点现在什么状态"的记录。

    对应的是 DAGNode（非 DBTNode）。v1 不做 DBT，单工具直接调用。
    """
    node_id: NodeId
    status: NodeStatus = NodeStatus.PENDING
    attempt: int = 0                       # 已尝试次数（0=尚未首次执行）
    started_at: str = ""                   # ISO 8601
    finished_at: str = ""
    artifact_refs: list[ArtifactId] = Field(default_factory=list)
    # ↑ ArtifactStore 中该节点完成后的产物引用列表。
    #   从 NodeResult.artifact_refs 复制而来（_apply_evaluation 的 continue 分支）。
    #   NodeResult 存"本次执行产生"，NodeState 存"节点最终关联"（checkpoint 用）。
    #   所有产物统一存 ArtifactStore，引用即 artifact_id。
    #   v1: 内存 dict，v2: 文件 / 数据库。
    summary: str = ""                      # 节点完成后的简短摘要
    error: str | None = None               # 人类可读的错误诊断（Evaluator 产出的 reason），
    #                                          retry / replan / abort 时写入。
    error_code: NodeErrorCode | None = None  # 机器可读错误码（来自 NodeResult.error），
    #                                          retry / replan / abort 时写入。
    #                                          ExperienceRecord / Planner 做结构化匹配用。


class TaskState(BaseModel):
    """SubAgent 管理：任务级运行时状态。

    SubAgent 创建并控制生命周期。Orchestrator 运行时读取 status/plan，
    写入 status（终态转换），但不拥有 TaskState。

    checkpoint 时与 ExecutionState 合并持久化。
    """
    task_id: TaskId
    status: TaskStatus = TaskStatus.PENDING
    plan: Plan | None = None                   # 当前生效的 Plan（Replan 后更新）
    tools: list[Tool] = Field(default_factory=list)  # 本次任务可用的工具列表（从 DelegationContext 透传，Executor 消费）
    created_at: str = ""                       # ISO 8601
    updated_at: str = ""
    error: str | None = None                   # 人类可读的错误原因（终态 FAILED 时写入）
    error_code: TaskErrorCode | None = None    # 机器可读的任务级错误码（终态 FAILED 时写入）

    def is_terminal(self) -> bool:
        """任务是否已到达终态。"""
        return self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )


class ExecutionPhase(StrEnum):
    """Orchestrator 主循环的执行阶段。

    比 TaskStatus 更细粒度——TaskStatus.RUNNING 内部有三个阶段轮转。
    """
    EXECUTING = "executing"
    EVALUATING = "evaluating"
    FINALIZING = "finalizing"


class ExecutionState(BaseModel):
    """Orchestrator 内部：Plan 执行层的运行时状态。

    Orchestrator 主循环中唯一可变的状态对象。
    每次循环迭代后更新，与 TaskState 合并后 checkpoint 持久化。

    SubAgent 不读 node_states 内容——它只负责在 checkpoint/resume 时传递。
    """
    node_states: dict[NodeId, NodeState] = Field(default_factory=dict)
    current_phase: ExecutionPhase = ExecutionPhase.EXECUTING
    waiting_for_approval: bool = False         # 等待审批回传
    pause_requested: bool = False          # 外部请求暂停（Orchestrator 检查后返回 PAUSED）
    created_at: str = ""                   # Orchestrator._init_exec_state() 时赋值
    updated_at: str = ""


# ── Checkpoint ──────────────────────────────────────────────────────────────────


class TaskCheckpoint(BaseModel):
    """任务执行快照——TaskState + ExecutionState 的组合容器。

    Orchestrator._build_checkpoint() 产出，CheckpointManager 持久化，
    SubAgent 在 resume / NEEDS_REPLAN / PAUSED 路径消费。

    两个字段均为 Pydantic 模型实例（非 dict）——checkpoint 在内存中保持强类型，
    CheckpointManager 在 save/load 边界做 model_dump / model_validate 转换。
    """
    task: TaskState
    exec: ExecutionState


# ── 节点产出契约 ──────────────────────────────────────────────────────────────

class OutputContract(BaseModel):
    """节点产出的结构化契约。

    Planner 在 DAGNode 上声明，Evaluator 用来判定 business_status。
    当前 v1 两字段紧凑实现，够用且边界清晰。

    TODO: 后续版本考虑拆分为四字段，覆盖从"产出什么"到"怎么校验"的完整链路：
        expected_outcome: str          — 人类可读说明（替代当前 description）
        output_contract: dict | None   — 机器可校验 JSON Schema（当前 json_schema）
        success_criteria: str          — 质量要求/门槛（LLM 语义评判，如"温度误差 ±2°C"）
        validation_rules: list[dict]   — 可执行校验规则（如 [{"path":"$.temp","op":"gte","value":-100}]）
        v1 暂不实施。JSON Schema 本身可延伸（minimum/maximum/pattern/enum），不会堵死路。

        output_type 为 v2 类型注册表预留——v1 留空；非空时走 OutputTypeRegistry 查预定义
        schema + 特化校验器。一条字段，零额外复杂度。

    例（v1 当前实现）：
        {
            "description": "返回北京未来3天天气，包含温度、湿度、降水概率",
            "json_schema": {
                "type": "object",
                "properties": {
                    "temperature": {"type": "number"},
                    "humidity": {"type": "number"},
                    "precipitation": {"type": "number"},
                },
                "required": ["temperature"],
            },
            "output_type": "",
        }
    """
    description: str = ""                           # 自然语言期望（LLM Evaluator 语义比对）
    json_schema: dict[str, Any] | None = None      # JSON Schema（程序化校验，可选）
    output_type: str = ""                          # v2 类型注册表 key，v1 留空


# ── DAG 与行为树 ────────────────────────────────────────────────────────────────

class DAGNode(BaseModel):
    """Planner 产出的单个 DAG 节点。

    只声明目标和期望结果，不包含执行策略——DBT 由 Executor 运行时生成。
    图结构（依赖关系）提升到 DAG.edges，节点不持有。
    数据流通过 input_bindings 声明（正交于 edges 执行顺序）。

    字段：
        node_id:         节点唯一标识
        description:     做什么（给 Executor 看的任务描述）
        output_contract: 期望产出什么（给 Evaluator 做 business_status 判定）
        required:        是否必须成功（False → 失败可跳过，不阻塞后续节点）
        input_bindings:  从 ArtifactStore 取的产物 ID 列表（数据依赖，正交于 edges 执行依赖）
        output_as:       产出物存在 ArtifactStore 中的 ArtifactId
        max_retries:     per-node 最大重试次数（None → 使用全局 ConstraintConfig.max_retries_per_node）
        max_duration_s:  per-node 最大执行时长秒数（None → 无限制，与 ConstraintConfig.max_task_duration_s 命名对齐）
    """
    node_id: NodeId
    description: str = ""                         # 节点目标描述
    output_contract: OutputContract = Field(default_factory=OutputContract)  # 期望产出 + 约束
    required: bool = True                         # 是否必须成功（False → 可跳过）
    input_bindings: list[ArtifactId] = Field(default_factory=list)   # 从 ArtifactStore 取哪些产物
    output_as: ArtifactId | None = None            # 产出存到 ArtifactStore 的 ID（None=无产物）
    max_retries: int | None = None                # None=使用全局默认
    max_duration_s: float | None = None           # None=无限制


class DAG(BaseModel):
    """有向无环图。

    edges 方向: (from, to) → from 必须先完成，to 才能开始。
    等价于 from 是 to 的前置依赖。
    """
    nodes: list[DAGNode] = Field(default_factory=list)
    edges: list[tuple[NodeId, NodeId]] = Field(default_factory=list)


class Plan(BaseModel):
    """Planner 产出的 DAG 骨架。

    纯数据结构。dag 持有完整的图信息（nodes + edges）。
    metadata（task_id）归 TaskState，委派上下文由 SubAgent 持有。

    字段：
        dag:          完整的 DAG（nodes + edges）
        plan_version: Replan 时递增
        created_at:   Planner.plan() 首次产出时赋值（ISO 8601）
        updated_at:   每次 Replan 后更新（ISO 8601）
    """
    dag: DAG = Field(default_factory=DAG)
    plan_version: int = 1                  # Replan 时递增
    created_at: str = ""                   # Planner 首次产出时赋值
    updated_at: str = ""                   # 每次 Replan 后更新


class DAGPatchType(StrEnum):
    """DAG 补丁类型——3 种。

    Planner.replan() 根据失败诊断选择合适的补丁策略。
    注意：full_replan 不属于 DAGPatch 职责——
    全量重建由 Sub-Agent 层直接创建新 Plan + 重建 PlanGraph。
    """
    REPLACE_NODES = "replace_nodes"  # 替换指定节点（replace_nodes 字段）
    ADD_NODES = "add_nodes"          # 仅新增节点（add_nodes 字段）
    REMOVE_NODES = "remove_nodes"    # 仅移除节点（remove_nodes 字段）


class DAGPatch(BaseModel):
    """Replan 产出的 DAG 增量修改。

    局部修改——不动已完成节点，只改失败/未执行的部分。
    五种操作按 apply_patch 顺序：replace → remove nodes → add nodes → remove edges → add edges。

    full_replan 不属于 DAGPatch 职责——
    全量重建由 Sub-Agent 层直接创建新 Plan + 重建 PlanGraph。

    字段：
        patch_type:     修改类型（元数据，primary intent）
        reason:         为什么需要修改（来自 Evaluator 诊断）
        trigger_nodes:  哪些失败节点触发了这次 Replan（并行执行时可能多个）
        created_at:     Planner.replan() 产出时赋值（ISO 8601）
        replace_nodes:  要替换的节点映射，(old_id, new_node)
        remove_nodes:   要移除的 node_id 列表（不包含 replace 中的 old_id）
        add_nodes:      要新增的节点列表（不包含 replace 中的 new_node）
        remove_edges:   要移除的边
        add_edges:      要新增的边
    """
    patch_type: DAGPatchType = DAGPatchType.REPLACE_NODES
    reason: str = ""
    trigger_nodes: list[NodeId] = Field(default_factory=list)
    created_at: str = ""
    replace_nodes: list[tuple[NodeId, DAGNode]] = Field(default_factory=list)
    remove_nodes: list[NodeId] = Field(default_factory=list)
    add_nodes: list[DAGNode] = Field(default_factory=list)
    remove_edges: list[tuple[NodeId, NodeId]] = Field(default_factory=list)
    add_edges: list[tuple[NodeId, NodeId]] = Field(default_factory=list)


# ── Plan 校验 ──────────────────────────────────────────────────────────────────

class ValidationIssueType(StrEnum):
    """Plan 校验问题类型。"""
    CYCLE = "cycle"                              # DAG 有环
    DUPLICATE_ID = "duplicate_id"                # node_id 重复
    MISSING_DEPENDENCY = "missing_dependency"    # edges 引用了不存在的节点
    UNKNOWN_TOOL = "unknown_tool"                # 工具名不在可用工具列表中
    INVALID_INPUT = "invalid_input"              # 节点 input 不满足工具 schema
    MISSING_CONTRACT = "missing_contract"        # output_contract 缺失或非法
    ORPHAN_NODE = "orphan_node"                  # 孤立节点（无依赖且无被依赖）
    UNREACHABLE_NODE = "unreachable_node"        # 不可达节点
    MISSING_CONFIRMATION = "missing_confirmation"  # 高风险节点无确认标记
    MAX_NODES_EXCEEDED = "max_nodes_exceeded"    # 节点数超限
    MAX_DEPTH_EXCEEDED = "max_depth_exceeded"    # DAG 深度超限


class ValidationIssue(BaseModel):
    """Plan 校验发现的单个问题。"""
    type: ValidationIssueType
    node_id: NodeId | None = None
    message: str = ""


class ValidationAction(StrEnum):
    """Plan 校验后的行动。"""
    PROCEED = "proceed"    # 校验通过，继续执行
    FIX_PLAN = "fix_plan"  # 有问题但可修复，重新规划
    ABORT = "abort"        # 不可修复，中止任务


class ValidationResult(BaseModel):
    """Plan 校验结果。"""
    valid: bool = False
    issues: list[ValidationIssue] = Field(default_factory=list)
    action: ValidationAction = ValidationAction.PROCEED


# ── 节点产物 ──────────────────────────────────────────────────────────────────

class Artifact(BaseModel):
    """节点产出的结构化数据，存于 ArtifactStore。

    所有产物统一走 ArtifactStore，NodeResult.artifact_refs 持有引用。
    artifact_id 与 DAGNode.output_as 对应——上游产出时以此 key 存入，
    下游通过 input_bindings 以此 key 取出。
    """
    artifact_id: ArtifactId
    artifact_type: str                         # 产物类型。v1 来自 tool_result.result_type（如 "text"），
                                                # v2 对齐 OutputContract.output_type 类型注册表 key
    node_id: NodeId
    task_id: TaskId
    content: Any = None                        # 实际产物数据——dict、list、str、bytes 等不做限制
    metadata: dict[str, Any] = Field(default_factory=dict)  # 附加信息：大小、格式、来源等
    created_at: str = ""                       # ISO 8601


class DBTNodeType(StrEnum):
    """DBT 节点类型。

    ⚠️ 实验性质。有降级路径：如果 DBT 表现不佳，
    降级为 NodeConfig(max_retries + fallback_tool + on_exhausted)。
    """
    SEQUENCE = "sequence"       # 顺序执行子节点直到全部成功
    SELECTOR = "selector"       # 尝试子节点直到一个成功
    PARALLEL = "parallel"       # 并发执行所有子节点
    ACTION = "action"           # 叶子节点：调用工具
    CONDITION = "condition"     # 分支判断


class DBTNode(BaseModel):
    """行为树节点。

    ⚠️ 实验性质。v1 可先走 LLM 生成，后续视效果决定是否保留或降级。
    """
    node_id: NodeId
    node_type: DBTNodeType
    tool_name: str | None = None         # ACTION 节点专用
    tool_params: dict[str, Any] = Field(default_factory=dict)  # ACTION 节点专用
    condition: str | None = None         # CONDITION 节点专用
    children: list[DBTNode] = Field(default_factory=list)
    max_retries: int = 1
    max_duration_s: float | None = None


# ── 节点执行/评估结果 ────────────────────────────────────────────────────────────────

class ExecutionStatus(StrEnum):
    """Executor 工具执行状态——5 态。

    工具是否正常返回？Orchestrator 据此决定 retry / skip / abort 等路由。
    """
    OK = "ok"                            # 工具正常返回
    TIMEOUT = "timeout"                  # 工具调用超时
    ERROR = "error"                      # 工具返回错误
    PERMISSION_DENIED = "permission_denied"  # 用户拒绝审批
    CANCELLED = "cancelled"              # 外部取消（任务级中断）


class BusinessStatus(StrEnum):
    """Evaluator 业务结果判定——3 态。

    Plan 中声明的期望结果是否达成？
    """
    GOAL_ACHIEVED = "goal_achieved"      # 完全达成
    GOAL_PARTIAL = "goal_partial"        # 部分达成
    GOAL_FAILED = "goal_failed"          # 未达成


class NodeErrorCode(StrEnum):
    """节点执行错误码——NodeResult.error 使用。

    只覆盖 Executor 层（单节点工具调用）的失败原因。
    任务级错误（PLANNING_FAILED、FAILURE_MELTDOWN 等）在 TaskErrorCode 中。
    """
    TOOL_ERROR = "tool_error"              # 工具返回错误（runtime error）
    TOOL_TIMEOUT = "tool_timeout"          # 工具调用超时（区别于 ExecutionStatus.TIMEOUT 的语义）
    TOOL_NOT_FOUND = "tool_not_found"      # Executor 无法匹配到合适的工具
    PERMISSION_DENIED = "permission_denied"  # 用户拒绝审批
    WORKER_CRASH = "worker_crash"          # Worker Agent 异常退出
    CONTEXT_OVERFLOW = "context_overflow"  # 单节点 prompt 超上下文窗口
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"  # 单节点 LLM 调用 token 超限
    NETWORK_ERROR = "network_error"        # 瞬态网络故障（可重试）


class EvaluatorAction(StrEnum):
    """Evaluator 建议的下一步动作——6 态。

    Orchestrator._apply_evaluation() 据此决定 NodeStatus 转换和执行流。
    并非所有 action 在所有上下文中都合法（如 skip 仅用于 required=False 的节点）。
    """
    CONTINUE = "continue"       # 节点成功，继续下一个
    RETRY = "retry"             # 重新入队（attempt+1 后重试）
    SKIP = "skip"               # 跳过（仅非必需节点，标记 SKIPPED 后继续）
    REPLAN = "replan"           # 结构性失败，触发 NEEDS_REPLAN
    ASK_APPROVAL = "ask_approval"  # 需要用户审批，触发 BLOCKED
    ABORT = "abort"             # 不可恢复，触发 FAILED


class NodeResult(BaseModel):
    """Executor 产出——工具执行的原始结果。

    v1 走单工具直接调用（不做 DBT）。所有产物统一存 ArtifactStore，
    NodeResult 只持有引用，不内联数据。

    Evaluator 不修改 NodeResult，而是产出 NodeResultEvaluation 包裹它。
    """
    node_id: NodeId
    artifact_refs: list[ArtifactId] = Field(default_factory=list)  # ArtifactStore 引用（含所有产物）
    error: NodeErrorCode | None = None                       # 节点级执行错误码
    attempt: int = 0                                        # 当前是第几次执行尝试。Executor 始终返回 0（不追踪重试），
                                                            # 实际值由 Orchestrator 在 _execute_nodes 中用 NodeState.attempt 覆盖。
    duration_ms: int = 0                                    # 本次工具调用耗时（毫秒）。Executor 填写，ExperienceRecord 消费


class NodeResultEvaluation(BaseModel):
    """Evaluator 产出——对 NodeResult 的双重判定。

    不修改 NodeResult。Evaluator 读 DAGNode.output_contract 和 ArtifactStore 中
    的实际产物，得出两个维度的判定，包裹原始 NodeResult 一起返回。
    """
    node_id: NodeId
    execution_status: ExecutionStatus = ExecutionStatus.OK   # 工具是否正常返回？（5 态）
    business_status: BusinessStatus = BusinessStatus.GOAL_ACHIEVED  # Plan 期望是否达成？（3 态）
    reason: str = ""                       # 判定依据（为什么判这个结果）
    suggested_action: EvaluatorAction = EvaluatorAction.CONTINUE  # Orchestrator 执行的下一步
    result: NodeResult                     # 被评定的原始执行结果


class TaskResult(BaseModel):
    """任务完成后的结构化结果。

    包含所有节点结果 + 任务级摘要。
    CognitiveLoop 根据 return_mode 决定用自然语言还是结构化数据向用户汇报。

    错误分工：
        任务级  → error (str) + error_code (TaskErrorCode)，来自 TaskState
        节点级  → node_results[node_id].reason + result.error，来自 NodeResultEvaluation
    """
    task_id: TaskId
    status: TaskStatus
    node_results: dict[NodeId, NodeResultEvaluation] = Field(default_factory=dict)
    summary: str = ""                           # 自然语言摘要
    created_at: str = ""                        # TaskState.created_at（ISO 8601）
    completed_at: str = ""                      # 终态时间（ISO 8601），duration = completed_at - created_at
    token_usage: dict[str, int] = Field(        # {"planner": N, "executor": N, "evaluator": N, ...}
        default_factory=dict
    )
    tools_used: list[str] = Field(default_factory=list)   # 审计：本任务调用了哪些工具名
    # 任务级错误（仅在 failed/cancelled 时有值）
    error: str | None = None                    # 人类可读，来自 TaskState.error
    error_code: TaskErrorCode | None = None     # 机器可读，来自 TaskState.error_code


# ── 经验记忆 ────────────────────────────────────────────────────────────────────

class ExperienceRecord(BaseModel):
    """单条任务经验记忆。

    存于 TaskSystem 专属 Milvus namespace，与用户 LTM 独立。

    ⚠️ v1 不做——经验记忆（ExperienceRecord / Reflector / Milvus 写入）
    属于渐进式学习闭环，v1 骨架阶段仅保留类型定义占位。
    字段和 Milvus schema 待 v2 敲定。
    """
    tool_name: str
    error_pattern: str
    params_snapshot: dict[str, Any] = Field(default_factory=dict)
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    diagnosis: str = ""
    fix: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    verified_count: int = 0
    success_after_apply: float = 0.0
    status: Literal["unverified", "verified", "deprecated"] = "unverified"
    occurrences: int = 0
    last_seen: str = ""                  # ISO 8601
