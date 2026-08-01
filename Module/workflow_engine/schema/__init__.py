"""WorkflowEngine 核心数据结构（内部类型）。

跨边类型（DelegationContext/SubmitError/HITLData/WorkflowId/WorkflowStatus 等）已随契约层
提取迁至 Module/workflow_contract，本模块不再 re-export——内部代码请从契约层解析。

所有类型通过此模块统一导出，保持向后兼容。
现有 `from Module.workflow_engine.schema import X` 无需修改（仅限仍归本模块的类型）。
"""

# ── types.py: 基础类型（引擎内部） ──
from .types import (
    ArtifactId,
    NodeErrorCode,
    NodeStatus,
    ResumeErrorCode,
    WorkflowErrorCode,
    # 工具（内部使用，仍通过此路径提供向后兼容）
    now_iso,
)

# ── protocol.py: 跨层协议（引擎本地剩余类型） ──
from .protocol import (
    ResumeError,
    UserProfile,
)

# ── output.py: 查询视图（事件已迁契约层） ──
from .output import (
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

# ── internal.py: 编排数据模型 ──
from .internal import (
    Artifact,
    BusinessStatus,
    DAG,
    DAGNode,
    DAGPatch,
    DAGPatchType,
    DBTNode,
    DBTNodeType,
    EvaluatorAction,
    ExecutionMode,
    ExecutionPhase,
    ExecutionState,
    ExecutionStatus,
    ExperienceRecord,
    NodeResult,
    NodeResultEvaluation,
    NodeState,
    OrchestratorResult,
    OrchestratorRunStatus,
    OutputContract,
    Plan,
    WorkflowCheckpoint,
    WorkflowResult,
    WorkflowState,
    ValidationAction,
    ValidationIssue,
    ValidationIssueType,
    ValidationResult,
)

# 内部工具不导出: _normalize_utc, IsoTimestamp
# parse_iso 已随契约层迁至 Module.workflow_contract.utils（Task 6）
# now_iso 因编排层 (orchestrator/sub_agent/task_system/parser) 大量使用，保留在公开导出中
# 已迁契约层的类型: AgentId/ApprovalId/NodeId/SessionId/WorkflowId/WorkflowStatus/
#   SubmitRejectionCode/RBACLevel/ConstraintConfig/WorkflowConstraints/ReturnMode/
#   DelegationContext/DelegationResult/DelegationStatus/SubmitError/HITLData/
#   WorkflowEvent/WorkflowEventType/Tool
