"""WorkflowEngine — 异步 Plan-Execute-Evaluate-Replan 任务编排模块。

独立于 CognitiveLoop，通过 WorkflowMailbox + HITLNotifier 协议与外界通信。

导出分组：
  - 公共 API:     WorkflowEngine, SubAgent, Orchestrator
  - 基础类型:     NodeId, WorkflowId, WorkflowStatus, WorkflowErrorCode, etc.
  - 本地协议:     ResumeError
  - 查询/输出:    WorkflowSummary, WorkflowDAG, TokenStats, AgentInfo, etc.
  - 内部编排:     Plan, DAGNode, DAGPatch, NodeResult, WorkflowState, WorkflowCheckpoint, etc.
  - 校验/产物:    ValidationIssue, OutputContract, Artifact, etc.

跨边类型（DelegationContext/SubmitError/HITLData/HITLNotifier/AgentInfra/WorkflowId 等）
已随契约层提取迁至 Module.workflow_contract，不再从本模块导出——消费方请从契约层解析。
"""

# ── 公共 API ──
from Module.workflow_engine.core.orchestrator import Orchestrator
from Module.workflow_engine.core.sub_agent import SubAgent
from Module.workflow_engine.workflow_engine import WorkflowEngine

# ── 基础类型（ID、状态、错误码） ──
from Module.workflow_engine.schema import (
    ArtifactId,
    BusinessStatus,
    EvaluatorAction,
    ExecutionStatus,
    NodeErrorCode,
    NodeStatus,
    ResumeErrorCode,
    WorkflowErrorCode,
)

# ── 本地协议（resume 错误） ──
from Module.workflow_engine.schema import (
    ResumeError,
)

# ── 查询 / 对外输出 ──
from Module.workflow_engine.schema import (
    AgentInfo,
    AgentList,
    AgentStatus,
    AgentType,
    DAGNodeInfo,
    WorkflowDAG,
    WorkflowProgress,
    WorkflowSummary,
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
    WorkflowResult,
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
    WorkflowCheckpoint,
    WorkflowState,
)

__all__ = [
    # 公共 API
    "Orchestrator",
    "SubAgent",
    "WorkflowEngine",
    # 基础类型
    "ArtifactId",
    "BusinessStatus",
    "EvaluatorAction",
    "ExecutionStatus",
    "NodeErrorCode",
    "NodeStatus",
    "ResumeErrorCode",
    "WorkflowErrorCode",
    # 本地协议
    "ResumeError",
    # 对外输出
    "AgentInfo",
    "AgentList",
    "AgentStatus",
    "AgentType",
    "DAGNodeInfo",
    "WorkflowDAG",
    "WorkflowProgress",
    "WorkflowSummary",
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
    "WorkflowResult",
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
    "WorkflowCheckpoint",
    "WorkflowState",
]
