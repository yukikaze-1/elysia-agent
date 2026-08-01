"""第二层：核心输出 —— WorkflowMailbox 事件 + 查询视图。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .types import (
    AgentId,
    IsoTimestamp,
    NodeErrorCode,
    NodeId,
    NodeStatus,
    WorkflowErrorCode,
    WorkflowId,
    WorkflowStatus,
    now_iso,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 第二层：核心输出 —— WorkflowMailbox 事件 + 查询视图
# ═══════════════════════════════════════════════════════════════════════════════

# ── WorkflowMailbox 事件 ────────────────────────────────────────────────────────────

class WorkflowEventType(StrEnum):
    """WorkflowMailbox 事件类型——6 种。"""
    PROGRESS = "progress"
    APPROVAL_NEEDED = "approval_needed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class WorkflowEvent(BaseModel):
    """WorkflowMailbox 中传输的事件。

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
    type: WorkflowEventType
    data: dict[str, Any] = Field(default_factory=dict)

    # ── data shape 参考 ──────────────────────────────────────────────────────

    # progress: {
    #     "task_id": WorkflowId,
    #     "node_id": NodeId,
    #     "node_description": str,  # 当前节点在做什么
    #     "percent": float,         # 0~1，整个任务的完成比例
    #     "message": str,           # 简短描述
    # }

    # approval_needed: {            # 与 HITLData 字段一一对应
    #     "task_id": WorkflowId,
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
    #     "task_id": WorkflowId,
    #     "result": str,            # 自然语言摘要
    #     "summary": str,           # 简短摘要
    #     "total_nodes": int,
    #     "completed_nodes": int,
    #     "failed_nodes": int,
    # }

    # failed: {
    #     "task_id": WorkflowId,
    #     "error": str,             # 人类可读错误描述
    #     "error_type": WorkflowErrorCode,  # 任务级错误码
    # }

    # cancelled: {
    #     "task_id": WorkflowId,
    #     "reason": str,            # 取消原因
    # }

    # paused: {
    #     "task_id": WorkflowId,
    #     "message": str,           # 暂停说明
    # }


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
