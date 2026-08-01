"""TaskSystem — 异步 Plan-Execute-Evaluate-Replan 任务编排模块。

独立于 CognitiveLoop，通过 TaskMailbox + HITLNotifier 协议与外界通信。

导出分组：
  - 公共 API:     TaskSystem, DelegationPreparer, SubAgent, Orchestrator, AgentInfra, HITLNotifier
  - 基础类型:     TaskId, NodeId, TaskStatus, TaskErrorCode, etc.
  - 跨层协议:     DelegationContext, HITLData, ConstraintConfig, SubmitError, etc.
  - 查询/输出:    TaskSummary, TaskDAG, TokenStats, AgentInfo, etc.
  - 内部编排:     Plan, DAGNode, DAGPatch, NodeResult, TaskState, TaskCheckpoint, etc.
  - 校验/产物:    ValidationIssue, OutputContract, Artifact, etc.
"""

# ── 公共 API ──
from Module.workflow_engine.core.orchestrator import Orchestrator
from Module.workflow_engine.core.sub_agent import SubAgent
from Module.workflow_engine.delegation_preparer import DelegationPreparer
from Module.workflow_engine.infra import AgentInfra
from Module.workflow_engine.protocol import HITLNotifier
from Module.workflow_engine.task_system import TaskSystem

# ── 基础类型（ID、状态、错误码） ──
from Module.workflow_engine.schema import (
    AgentId,
    ApprovalId,
    ArtifactId,
    BusinessStatus,
    EvaluatorAction,
    ExecutionStatus,
    NodeErrorCode,
    NodeId,
    NodeStatus,
    RBACLevel,
    ResumeErrorCode,
    SessionId,
    SubmitRejectionCode,
    TaskErrorCode,
    TaskId,
    TaskStatus,
)

# ── 跨层协议（CognitiveLoop ↔ TaskSystem） ──
from Module.workflow_engine.schema import (
    ConstraintConfig,
    DelegationContext,
    DelegationResult,
    DelegationStatus,
    HITLData,
    ReturnMode,
    ResumeError,
    SubmitError,
    TaskConstraints,
    # CognitiveLoop 侧类型（TaskSystem 引用，不拥有）
    Tool,
    UserProfile,
)

# ── 查询 / 对外输出 ──
from Module.workflow_engine.schema import (
    AgentInfo,
    AgentList,
    AgentStatus,
    AgentType,
    DAGNodeInfo,
    TaskDAG,
    TaskEvent,
    TaskEventType,
    TaskProgress,
    TaskSummary,
    TokenComponent,
    TokenComponentType,
    TokenPeriod,
    TokenStats,
    TokenUsage,
)

# ── 内部编排（规划 / 执行 / 状态） ──
from Module.workflow_engine.schema import (
    DAG,
    DAGNode,
    DAGPatch,
    DAGPatchType,
    DBTNode,
    DBTNodeType,
    ExperienceRecord,
    NodeResult,
    NodeResultEvaluation,
    Plan,
    TaskResult,
)

# ── Plan 校验 ──
from Module.workflow_engine.schema import (
    ValidationAction,
    ValidationIssue,
    ValidationIssueType,
    ValidationResult,
)

# ── 节点产物 ──
from Module.workflow_engine.schema import (
    Artifact,
    OutputContract,
)

# ── Orchestrator / Sub-Agent 运行时状态 ──
from Module.workflow_engine.schema import (
    ExecutionPhase,
    ExecutionState,
    NodeState,
    OrchestratorResult,
    OrchestratorRunStatus,
    TaskCheckpoint,
    TaskState,
)

__all__ = [
    # 公共 API
    "AgentInfra",
    "DelegationPreparer",
    "HITLNotifier",
    "Orchestrator",
    "SubAgent",
    "TaskSystem",
    # 基础类型
    "AgentId",
    "ApprovalId",
    "ArtifactId",
    "BusinessStatus",
    "EvaluatorAction",
    "ExecutionStatus",
    "NodeErrorCode",
    "NodeId",
    "NodeStatus",
    "RBACLevel",
    "ResumeErrorCode",
    "SessionId",
    "SubmitRejectionCode",
    "TaskErrorCode",
    "TaskId",
    "TaskStatus",
    # 跨层协议
    "ConstraintConfig",
    "DelegationContext",
    "DelegationResult",
    "DelegationStatus",
    "HITLData",
    "ResumeError",
    "ReturnMode",
    "SubmitError",
    "TaskConstraints",
    "Tool",
    "UserProfile",
    # 对外输出
    "AgentInfo",
    "AgentList",
    "AgentStatus",
    "AgentType",
    "DAGNodeInfo",
    "TaskDAG",
    "TaskEvent",
    "TaskEventType",
    "TaskProgress",
    "TaskSummary",
    "TokenComponent",
    "TokenComponentType",
    "TokenPeriod",
    "TokenStats",
    "TokenUsage",
    # 内部编排
    "DAG",
    "DAGNode",
    "DAGPatch",
    "DAGPatchType",
    "DBTNode",
    "DBTNodeType",
    "ExperienceRecord",
    "NodeResult",
    "NodeResultEvaluation",
    "Plan",
    "TaskResult",
    # Plan 校验
    "ValidationAction",
    "ValidationIssue",
    "ValidationIssueType",
    "ValidationResult",
    # 节点产物
    "Artifact",
    "OutputContract",
    # 运行时状态
    "ExecutionPhase",
    "ExecutionState",
    "NodeState",
    "OrchestratorResult",
    "OrchestratorRunStatus",
    "TaskCheckpoint",
    "TaskState",
]
