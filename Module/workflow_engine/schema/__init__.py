"""WorkflowEngine 核心数据结构。

所有类型通过此模块统一导出，保持向后兼容。
现有 `from Module.workflow_engine.schema import X` 无需修改。
"""

# ── types.py: 基础类型 ──
from .types import (
    # NewType 别名
    AgentId,
    ApprovalId,
    ArtifactId,
    NodeId,
    SessionId,
    WorkflowId,
    # 枚举
    NodeErrorCode,
    NodeStatus,
    RBACLevel,
    ResumeErrorCode,
    SubmitRejectionCode,
    WorkflowErrorCode,
    WorkflowStatus,
    # 工具（内部使用，仍通过此路径提供向后兼容）
    now_iso,
    parse_iso,
)

# ── protocol.py: 跨层协议 ──
from .protocol import (
    ConstraintConfig,
    DelegationContext,
    DelegationResult,
    DelegationStatus,
    HITLData,
    ReturnMode,
    ResumeError,
    SubmitError,
    WorkflowConstraints,
    Tool,
    UserProfile,
)

# ── output.py: 事件 + 查询视图 ──
from .output import (
    AgentInfo,
    AgentList,
    AgentStatus,
    AgentType,
    DAGNodeInfo,
    WorkflowDAG,
    WorkflowEvent,
    WorkflowEventType,
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

# 内部工具不导出: _normalize_utc, parse_iso, IsoTimestamp
# now_iso 因编排层 (orchestrator/sub_agent/task_system/parser) 大量使用，保留在公开导出中
