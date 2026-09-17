"""第二层：核心输出 —— 查询视图。

WorkflowMailbox 事件类型（WorkflowEvent/WorkflowEventType）已随契约层提取
迁至 Module/workflow_contract/schema.py，本模块从契约层解析。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from Module.workflow_contract import (
    AgentId,
    NodeId,
    WorkflowId,
    WorkflowStatus,
)

from Module.workflow_engine.schema.types import IsoTimestamp, NodeErrorCode, NodeStatus, WorkflowErrorCode, now_iso


# ═══════════════════════════════════════════════════════════════════════════════
# 第二层：核心输出 —— 查询视图
# ═══════════════════════════════════════════════════════════════════════════════

# ── 查询视图：任务摘要 ──────────────────────────────────────────────────────────

class WorkflowSummary(BaseModel):
    """任务完成后/查询时的摘要视图。

    Debug WebUI 和用户查询（"刚才那个任务怎么样了？"）使用。
    字段偏展示——tools_used 用字符串名，errors 用扁平字符串列表
    而非结构化 error+error_code。
    """
    task_id: WorkflowId
    status: WorkflowStatus
    intent: str
    created_at: IsoTimestamp = Field(default_factory=now_iso)                  # ISO 8601，对齐 WorkflowState.created_at
    duration_ms: int = 0                  # 总耗时（查询视图预计算，方便直接展示）
    nodes: dict[str, int] = Field(        # {"total": N, "completed": N, "failed": N, "skipped": N}
        default_factory=lambda: {"total": 0, "completed": 0, "failed": 0, "skipped": 0}
    )
    tools_used: list[str] = Field(default_factory=list)   # 审计：本任务调用了哪些工具名
    token_usage: dict[str, int] = Field(  # {"planner": N, "executor": N, "evaluator": N, ...}
        default_factory=dict
    )
    token_total: int = 0                    # 任务总 token 消耗（来自 TokenLedger.total_tokens）
    approvals: dict[str, int] = Field(    # {"requested": N, "approved": N, "denied": N}
        default_factory=lambda: {"requested": 0, "approved": 0, "denied": 0}
    )
    error: str | None = None                    # 任务级错误消息（来自 WorkflowState.error，对齐 WorkflowResult）
    error_code: WorkflowErrorCode | None = None     # 任务级错误码（来自 WorkflowState.error_code）
    # 节点级诊断见 WorkflowResult.node_results
    replans: int = 0


# ── 查询视图：进度 ──────────────────────────────────────────────────────────────

class WorkflowProgress(BaseModel):
    """单任务执行进度快照。"""
    task_id: WorkflowId
    status: WorkflowStatus = WorkflowStatus.PENDING
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


class WorkflowDAG(BaseModel):
    """任务 DAG 的可视化数据（Debug WebUI 渲染用）。"""
    nodes: list[DAGNodeInfo] = Field(default_factory=list)
    edges: list[tuple[NodeId, NodeId]] = Field(default_factory=list)


# ── 查询视图：Agent 池状态 ──────────────────────────────────────────────────────

class AgentType(StrEnum):
    """Agent 类型。"""
    SUB = "sub"        # Sub-Agent：任务级执行单元
    WORKER = "worker"  # Worker Agent：节点级执行单元


class AgentStatus(StrEnum):
    """Agent 运行时状态——WorkflowStatus 的子集（只含 Agent 可能的状态）。"""
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_APPROVAL = "awaiting_approval"


class AgentInfo(BaseModel):
    """单个 Agent 的运行信息。"""
    agent_id: AgentId
    agent_type: AgentType
    task_id: WorkflowId
    node_id: NodeId | None = None          # Worker 专用
    status: AgentStatus = AgentStatus.RUNNING
    started_at: IsoTimestamp = Field(default_factory=now_iso)
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
    task_id: WorkflowId
    components: list[TokenComponent] = Field(default_factory=list)


class TokenStats(BaseModel):
    """聚合 token 统计（按时间段）。"""
    period: TokenPeriod = TokenPeriod.TODAY
    total_tokens: int = 0
    by_component: dict[str, int] = Field(default_factory=dict)  # TokenComponentType → 总量
    by_model: dict[str, int] = Field(default_factory=dict)
