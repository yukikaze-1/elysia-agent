"""TaskSystem 核心数据结构。

分层：
  第一层 跨层协议 —— CognitiveLoop ↔ TaskSystem 的输入/输出契约
  第二层 核心输出 —— TaskSystem → Agent 的事件流 + 查询视图
  第三层 内部编排 —— Planner / Executor / Evaluator 的内部数据模型（待定）

所有类型使用 Pydantic BaseModel，在 TaskSystem 内部保持类型安全。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, NewType

from pydantic import BaseModel, Field

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


# ═══════════════════════════════════════════════════════════════════════════════
# 基础枚举
# ═══════════════════════════════════════════════════════════════════════════════

class TaskStatus(StrEnum):
    """任务状态机——11 态。

    TaskSystem 内部状态流转：
    PENDING → PLANNING → RUNNING → COMPLETED / FAILED / CANCELLED
                        RUNNING → PAUSE_REQUESTED → PAUSED → RESUMING → RUNNING
                        RUNNING → CANCEL_REQUESTED → CANCELLED
                        RUNNING → WAITING_APPROVAL → RUNNING
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


class TaskErrorCode(StrEnum):
    """submit() 拒绝时的错误码。

    调用方（CognitiveLoop）据此生成用户可见的错误信息。
    """
    INVALID_PARAM = "invalid_param"               # 参数校验不通过
    PLAN_TOO_LARGE = "plan_too_large"             # DAG 节点数超限
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"  # 上下文窗口不足
    FAILURE_MELTDOWN = "failure_meltdown"         # 资源耗尽，禁止 Replan
    MUTATION_LIMIT_REACHED = "mutation_limit_reached"   # 敏感操作次数达上限
    TIMEOUT = "timeout"                           # 任务整体超时
    PERMISSION_DENIED = "permission_denied"       # 审批被拒
    TOOL_ERROR = "tool_error"                     # 工具执行错误
    CANCELLED_BY_USER = "cancelled_by_user"       # 用户主动取消


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
    """DAG 节点的运行时状态。

    Orchestrator 主循环中 NodeState 使用，DAGNodeInfo（查询视图）复用。
    """
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNRECOVERABLE = "unrecoverable"


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
    DelegationPreparer 产出的 TaskConstraints 与其取 min 后生效。

    字段：
        max_total_tokens:     整个任务 LLM 调用总 token 上限（硬）
        max_context_tokens:   单次 prompt 最大 token 数（硬）
        max_failure_count:    连续失败 N 次后熔断（硬）
        max_dag_nodes:        DAG 节点数上限（硬）
        max_retries_per_node: 单节点最大重试次数（硬）
        max_mutations:        敏感操作次数上限（硬）
        max_task_duration_s:  整个任务最大执行时间（秒）（硬）
        max_tool_error_chars: 工具错误消息截断长度（软）
    """
    max_total_tokens: int       = Field(default=100_000,  ge=0)
    max_context_tokens: int     = Field(default=32_000,   ge=0)
    max_failure_count: int      = Field(default=5,         ge=1)
    max_dag_nodes: int          = Field(default=20,        ge=1)
    max_retries_per_node: int   = Field(default=3,         ge=0)
    max_mutations: int          = Field(default=10,        ge=0)
    max_task_duration_s: float  = Field(default=600.0,     ge=0)  # 10 分钟
    max_tool_error_chars: int   = Field(default=2000,      ge=0)


class TaskConstraints(BaseModel):
    """任务级约束（DelegationPreparer 产出）。

    与全局 ConstraintConfig 取 min 后生效。
    只设两个值——其余约束用全局就够了。

    字段：
        max_dag_nodes:   本次任务 DAG 步数上限
        max_mutations:   本次任务敏感操作次数上限
    """
    max_dag_nodes: int = Field(default=10, ge=1)
    max_mutations: int = Field(default=5,  ge=0)


# ── CognitiveLoop 侧类型（TaskSystem 只引用，不拥有）─────────────────────────────

# TODO: 以下两个类型属于 CognitiveLoop，最终定义在 Core 层。
# DelegationPreparer 暂时在此引用占位，待 CognitiveLoop 侧实现后替换。

class UserProfile(BaseModel):
    """用户画像（CognitiveLoop 管理）。

    DelegationPreparer 读取此信息来理解用户偏好和限制，
    但不修改——只读消费。

    TODO: 字段由 CognitiveLoop 侧定义。当前占位，防止裸 dict。
    """
    model_config = {"extra": "allow"}  # 暂时允许任意字段


class Tool(BaseModel):
    """工具描述（CognitiveLoop / ToolRegistry 管理）。

    全量工具列表中单个工具的抽象——名称、描述、参数 schema 等。
    DelegationPreparer 从全量列表中筛选相关工具。

    TODO: 字段由 ToolRegistry / ToolPlugin 侧定义。当前占位，防止裸 dict。
    """
    model_config = {"extra": "allow"}  # 暂时允许任意字段


# ── 委派上下文 ──────────────────────────────────────────────────────────────────

class DelegationContext(BaseModel):
    """DelegationPreparer 产出的委派沙盒。

    Planner 在这个沙盒内工作——只能看到 DelegationPreparer 筛选过的工具和上下文。

    所有字段由 DelegationPreparer.prepare() 填充，
    传入 TaskSystem.submit() 后由 Planner 直接消费。

    字段：
        intent:              LLM 输出的完整任务意图（非用户原始输入）
        hints:               LLM 观察到的元信息（紧急程度、偏好等），可为 None
        capabilities:        本次任务可用的工具列表（从全量工具中筛选）
        task_constraints:    本次任务级限制
        summarized_context:  LLM 摘要的相关上下文（≤ 500 字）
        session_id:          关联会话 ID，用于日志和 Debug 追踪
    """
    intent: str
    hints: str | None = None
    capabilities: list[Tool] = Field(default_factory=list)
    task_constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    summarized_context: str = ""
    session_id: SessionId


class DelegationResult(BaseModel):
    """DelegationPreparer.prepare() 的产出。

    status 决定 context 是否有值：
        ok               → context 有值，调用 TaskSystem.submit()
        no_matching_tools → context=None，工具库缺所需工具
        too_vague        → context=None，意图模糊需用户澄清

    字段：
        status:              三态判定结果
        context:             status=ok 时的委派上下文
        reasoning:           LLM 判定理由（始终有，用于调试）
        missing_capabilities:status=no_matching_tools 时，缺少的工具类型名
        suggestion:          status=too_vague 时，给用户的澄清建议
    """
    status: Literal["ok", "no_matching_tools", "too_vague"]
    context: DelegationContext | None = None
    reasoning: str = ""
    missing_capabilities: list[str] = Field(default_factory=list)
    suggestion: str | None = None

    # TODO: UserProfile / Tool 字段由 CognitiveLoop 侧定义，当前占位。
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
    code: TaskErrorCode
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
    message: str       # 人类可读的错误描述
    details: dict[str, Any] = Field(default_factory=dict)


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
        risk_summary:  风险摘要（操作类型、影响范围、参数预览）
    """
    task_id: TaskId
    approval_id: ApprovalId
    agent_id: AgentId
    node_id: NodeId
    node_task: str                       # 当前节点的目标任务描述
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    risk_summary: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# 第二层：核心输出 —— TaskMailbox 事件 + 查询视图
# ═══════════════════════════════════════════════════════════════════════════════

# ── TaskMailbox 事件 ────────────────────────────────────────────────────────────

class TaskEvent(BaseModel):
    """TaskMailbox 中传输的事件。

    type 决定 data 的 shape：
        progress:         进度更新
        approval_needed:  审批请求（审计流副本，快通道由 HITLNotifier 推送）
        completed:        任务成功完成
        failed:           任务失败
        cancelled:        任务被取消

    字段：
        type: 事件类型
        data: 事件负载，shape 随 type 变化（见下方 _*Schema 内部模型）
    """
    type: Literal["progress", "approval_needed", "completed", "failed", "cancelled"]
    data: dict[str, Any] = Field(default_factory=dict)

    # data shape 参考（不强制校验，因为不同来源的填充程度不同）：

    # progress: {
    #     "task_id": TaskId,
    #     "node_id": str,
    #     "node_index": "2/5",
    #     "node_description": str,  # 当前节点的人类可读描述
    #     "percent": float,         # 0~1，如果可估算
    #     "message": str,
    # }
    #
    # approval_needed: {
    #     "approval_id": str,
    #     "task_id": str,
    #     "agent_id": str,
    #     "node_id": str,
    #     "node_task": str,        # 当前节点在做什么
    #     "tool_name": str,
    #     "params": dict,
    #     "risk_summary": str,
    # }
    #
    # completed: {
    #     "result": str,           # 任务结果（自然语言或结构化 JSON 字符串）
    #     "summary": str,          # 简短摘要
    #     # TODO: 字段待第三层（内部编排）讨论后细化——
    #     #   - 是否需要 task_result: TaskResult 结构化引用？
    #     #   - summary 长度限制？
    # }
    #
    # failed: {
    #     "error": str,
    #     "error_type": str,       # TaskErrorCode 值
    #     # TODO: 字段待第三层（内部编排）讨论后细化——
    #     #   - 是否需要 failed_node_id？
    #     #   - 是否需要 node_results（已完成节点的部分结果）？
    # }
    #
    # cancelled: {}               # 空
    # TODO: 字段待第三层讨论后细化——
    #   - 是否需要 cancelled_reason？
    #   - 是否需要 partial_results（取消前已完成的部分）？


# ── 查询视图：任务摘要 ──────────────────────────────────────────────────────────

class TaskSummary(BaseModel):
    """任务完成后/查询时的摘要视图。

    Debug WebUI 和用户查询（"刚才那个任务怎么样了？"）使用。
    """
    task_id: TaskId
    status: TaskStatus
    intent: str
    started_at: str = ""          # ISO 8601
    duration_ms: int = 0
    nodes: dict[str, int] = Field(   # {"total": N, "completed": N, "failed": N, "skipped": N}
        default_factory=lambda: {"total": 0, "completed": 0, "failed": 0, "skipped": 0}
    )
    tools_used: list[str] = Field(default_factory=list)
    token_usage: dict[str, int] = Field(  # {"planner": N, "executor": N, "evaluator": N, ...}
        default_factory=dict
    )
    approvals: dict[str, int] = Field(   # {"requested": N, "approved": N}
        default_factory=lambda: {"requested": 0, "approved": 0}
    )
    errors: list[str] = Field(default_factory=list)
    replans: int = 0


# ── 查询视图：进度 ──────────────────────────────────────────────────────────────

class TaskProgress(BaseModel):
    """单任务执行进度快照。"""
    task_id: TaskId
    current_node_id: NodeId | None = None
    completed: int = 0
    total: int = 0
    elapsed_ms: int = 0
    estimated_remaining_ms: int | None = None


# ── 查询视图：DAG ───────────────────────────────────────────────────────────────

class DAGNodeInfo(BaseModel):
    """DAG 图中单个节点的展示信息（查询用，非内部编排数据）。"""
    node_id: NodeId
    description: str = ""
    status: NodeStatus = NodeStatus.PENDING
    tool_name: str | None = None
    progress: float = 0.0
    error: str | None = None


class TaskDAG(BaseModel):
    """任务 DAG 的可视化数据（Debug WebUI 渲染用）。"""
    nodes: list[DAGNodeInfo] = Field(default_factory=list)
    edges: list[tuple[str, str]] = Field(default_factory=list)   # [(from_node, to_node), ...]


# ── 查询视图：Agent 池状态 ──────────────────────────────────────────────────────

class AgentInfo(BaseModel):
    """单个 Agent 的运行信息。"""
    agent_id: AgentId
    agent_type: Literal["sub", "worker"]   # Sub-Agent / Worker Agent
    task_id: TaskId
    node_id: NodeId | None = None          # Worker 专用
    status: Literal["running", "paused", "awaiting_approval"] = "running"
    started_at: float = 0.0                # 启动时间戳
    last_seen_ts: float = 0.0
    duration_ms: int = 0


class AgentList(BaseModel):
    """Agent 池当前状态。"""
    sub_agents: list[AgentInfo] = Field(default_factory=list)
    worker_agents: list[AgentInfo] = Field(default_factory=list)
    total_capacity: int = 0
    available_slots: int = 0


# ── 查询视图：Token 统计 ────────────────────────────────────────────────────────

class TokenComponent(BaseModel):
    """单个组件的 token 消耗记录。"""
    component: str             # "planner" | "executor" | "evaluator" | "reflector"
    node_id: NodeId | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""


class TokenUsage(BaseModel):
    """单个任务的 token 消耗明细。"""
    task_id: TaskId
    components: list[TokenComponent] = Field(default_factory=list)


class TokenStats(BaseModel):
    """聚合 token 统计（按时间段）。"""
    period: Literal["today", "week", "all"] = "today"
    total_tokens: int = 0
    by_component: dict[str, int] = Field(default_factory=dict)
    by_model: dict[str, int] = Field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════════
# 第三层：内部编排 —— Planner / Executor / Evaluator 数据模型
# ═══════════════════════════════════════════════════════════════════════════════
# ↓ 下面这些字段待敲定，先留骨架 ↓


# ── Orchestrator 运行状态 ─────────────────────────────────────────────────────

class RunStatus(StrEnum):
    """Orchestrator.run() 的返回状态。

    Orchestrator 负责驱动 Plan 直到完成或遇到结构性障碍。
    遇到障碍时返回非 SUCCESS 状态，由 Sub-Agent 做决策。
    """
    SUCCESS = "success"            # 所有 required 节点完成
    FAILED = "failed"              # 不可恢复（资源耗尽、内部异常）
    NEEDS_REPLAN = "needs_replan"  # 结构性失败——DAG 走不通，需要修改 Plan
    BLOCKED = "blocked"            # 等待用户审批回传


class OrchestratorResult(BaseModel):
    """Orchestrator.run() 的返回值。

    SUCCESS 时 checkpoint_state 有最终快照。
    NEEDS_REPLAN 时 error_context 包含失败节点和诊断信息。
    BLOCKED 时等待外部审批回传。
    """
    status: RunStatus
    error_context: dict[str, Any] = Field(default_factory=dict)
    # ↑ NEEDS_REPLAN 时包含: {"node_id": NodeId, "reason": str, "failed_nodes": list[NodeId]}
    checkpoint_state: dict[str, Any] = Field(default_factory=dict)
    # ↑ 当前 TaskState 的可序列化快照，Sub-Agent 用于恢复或 Replan 上下文


# ── Orchestrator 状态追踪 ─────────────────────────────────────────────────────────

class NodeState(BaseModel):
    """Orchestrator 内部：单个节点的运行时状态。

    与 DAGNode（声明式）和 NodeResult（Evaluator 产出）不同——
    NodeState 是 Orchestrator 主循环中追踪"这个节点现在什么状态"的记录。

    对应的是 DAGNode（非 DBTNode）。DBT 是 Executor 内部实现细节，
    Orchestrator 不感知 DBT 层的状态。
    """
    node_id: NodeId
    status: NodeStatus = NodeStatus.PENDING
    attempt: int = 0                       # 已尝试次数（含当前）
    started_at: str = ""                   # ISO 8601（可读 + JSON 友好；若性能敏感可加 _duration_ms 缓存）
    finished_at: str = ""
    artifact_refs: list[str] = Field(default_factory=list)
    # ↑ ArtifactStore 中存储的大产物 ID 列表。
    #   小结果（< 几 KB）inline 在 NodeResult.output 中，
    #   大结果（搜索结果全文、文档、表格）存入 ArtifactStore，
    #   NodeState 只存引用，不存内容。
    #   v1: 内存 dict，引用即 artifact_id。
    #   v2: 考虑文件 / 数据库后端。
    summary: str = ""                      # 节点完成后的简短摘要
    error: str | None = None


class TaskState(BaseModel):
    """Orchestrator 内部：任务级运行时状态。

    Orchestrator 主循环中唯一可变的状态对象。
    每次循环迭代后更新，checkpoint 时持久化。
    """
    task_id: TaskId
    status: TaskStatus = TaskStatus.PENDING
    plan: Plan | None = None               # 当前生效的 Plan
    plan_version: int = 1                  # Replan 时递增
    node_states: dict[str, NodeState] = Field(default_factory=dict)
    current_phase: Literal["planning", "executing", "evaluating", "replanning", "finalizing"] = "planning"
    created_at: str = ""                   # ISO 8601
    updated_at: str = ""
    waiting_for_user: bool = False         # 等待审批回传

    def is_terminal(self) -> bool:
        """任务是否已到达终态。"""
        return self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )


# ── DAG 与行为树 ────────────────────────────────────────────────────────────────

class DAGNode(BaseModel):
    """Planner 产出的单个 DAG 节点。

    只声明目标和期望结果，不包含执行策略——DBT 由 Executor 运行时生成。
    """
    node_id: NodeId
    description: str = ""               # 节点目标描述
    expected_outcome: str = ""          # 期望结果（Evaluator 比对用）
    depends_on: list[NodeId] = Field(default_factory=list)  # 依赖的 node_id 列表
    required: bool = True               # 是否必须成功（False → 可跳过）


class Plan(BaseModel):
    """Planner 产出的 DAG 骨架。

    TaskSystem 内部使用。node_results 在执行过程中逐步填充。
    """
    task_id: TaskId
    plan_version: int = 1                      # Replan 时递增
    nodes: list[DAGNode] = Field(default_factory=list)
    context: DelegationContext | None = None  # 本次规划的委派上下文
    replan_count: int = 0                     # 已重规划次数


class DAGPatch(BaseModel):
    """Replan 产出的 DAG 增量修改。

    局部修改——不动已完成节点，只改失败/未执行的部分。
    full_replan 是兜底：局部修改无法覆盖时退回全量重规划。

    字段：
        patch_type:    修改类型
        reason:        为什么需要修改（来自 Evaluator 诊断）
        remove_nodes:  要移除的 node_id 列表
        add_nodes:     要新增的节点列表
        update_edges:  要新增/替换的边
    """
    patch_type: Literal["replace_nodes", "add_nodes", "remove_nodes", "full_replan"]
    reason: str = ""
    remove_nodes: list[NodeId] = Field(default_factory=list)
    add_nodes: list[DAGNode] = Field(default_factory=list)
    update_edges: list[tuple[str, str]] = Field(default_factory=list)


# ── Plan 校验 ──────────────────────────────────────────────────────────────────

class ValidationIssueType(StrEnum):
    """Plan 校验问题类型。"""
    CYCLE = "cycle"                              # DAG 有环
    DUPLICATE_ID = "duplicate_id"                # node_id 重复
    MISSING_DEPENDENCY = "missing_dependency"    # depends_on 引用不存在的节点
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


class ValidationResult(BaseModel):
    """Plan 校验结果。"""
    valid: bool = False
    issues: list[ValidationIssue] = Field(default_factory=list)
    action: Literal["proceed", "fix_plan", "abort"] = "proceed"


# ── 节点产物 ──────────────────────────────────────────────────────────────────

class Artifact(BaseModel):
    """节点产出的结构化数据，存于 ArtifactStore。

    与 NodeResult.output（小结果内联）互补。
    """
    artifact_id: str
    artifact_type: str                         # 对应 OutputContract.artifact_type
    node_id: NodeId
    task_id: TaskId
    content: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
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
    timeout_s: float | None = None


# ── 节点结果 ────────────────────────────────────────────────────────────────────

class NodeResult(BaseModel):
    """Evaluator 双重判定后的节点结果。

    execution_status: 工具是否正常返回？
    business_status:  Plan 中声明的期望结果是否达成？

    例：搜索返回 "没有结果" →
        execution_status="ok"（HTTP 200）、business_status="goal_failed"（没拿到信息）
    """
    node_id: NodeId
    execution_status: Literal["ok", "timeout", "error", "permission_denied"] = "ok"
    business_status: Literal["goal_achieved", "goal_partial", "goal_failed"] = "goal_achieved"
    evidence: str = ""                   # 判定依据（工具返回了什么）
    artifact_refs: list[str] = Field(default_factory=list)  # ArtifactStore 引用
    output: dict[str, Any] = Field(default_factory=dict)   # 小结果内联
    error: str | None = None


class TaskResult(BaseModel):
    """任务完成后的结构化结果。

    包含所有节点结果 + 任务级摘要。
    CognitiveLoop 根据 return_mode 决定用自然语言还是结构化数据向用户汇报。
    """
    task_id: TaskId
    status: TaskStatus
    node_results: dict[str, NodeResult] = Field(default_factory=dict)
    summary: str = ""                    # 自然语言摘要
    errors: list[str] = Field(default_factory=list)


# ── 经验记忆 ────────────────────────────────────────────────────────────────────

class ExperienceRecord(BaseModel):
    """单条任务经验记忆。

    存于 TaskSystem 专属 Milvus namespace，与用户 LTM 独立。
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
