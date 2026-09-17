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
# now_iso 因编排层 (orchestrator/sub_agent/workflow_engine/parser) 大量使用，保留在公开导出中
# 已迁契约层的类型: AgentId/ApprovalId/NodeId/SessionId/WorkflowId/WorkflowStatus/
#   SubmitRejectionCode/ConstraintConfig/WorkflowConstraints/ReturnMode/（同左）
#   DelegationContext/DelegationResult/DelegationStatus/SubmitError/HITLData/（同左）
#   WorkflowEvent/WorkflowEventType/Tool（同左）
# RBACLevel 已归位 Module/tool/capability.py（Task 8）——契约层 HITLData 延迟引用
# UserProfile 已归位 core/cognitive/user_profile.py（Task 8）——CognitiveLoop 拥有

# 公开面逐名声明（2026-09-17 Batch 2.3）：本包是「单一装配点」，re-export 是
# **有意**的公开 API。显式 `__all__` 让它从「碰巧没被用到」变成「声明导出」——
# 这正是 F401 判据要求的区分（否则只能靠 noqa 糊过去）。

# 冻结的公共面：显式 `__all__`（2026-09-17 Batch 2.3，理由见该批次记录）。
# 内容 = 全仓有显式导入的名字，逐名核对过。

# 冻结的公共面：显式 `__all__`（2026-09-17 Batch 2.3）。
# 内容 = 全仓有显式导入的名字 ∪ 被外部按名导入的子模块——逐名核对过；
# 新增公开名请一并加进来（F401 靠它区分「有意重导出」与「真没用」）。

# 冻结的公共面：显式 `__all__`（2026-09-17 Batch 2.3）。
# 内容 = 全仓有显式导入的名字（含被按名导入的子模块）——逐名核对过；
# 新增公开名请一并加进来（F401 靠它区分「有意重导出」与「真没用」）。

# 冻结的公共面：显式 `__all__`（2026-09-17 Batch 2.3）。
# 内容 = 全仓有显式导入的名字（含被按名导入的子模块）——逐名核对过；
# 新增公开名请一并加进来（F401 靠它区分「有意重导出」与「真没用」）。
__all__ = [
    "AgentInfo",
    "AgentList",
    "AgentStatus",
    "AgentType",
    "Artifact",
    "ArtifactId",
    "BusinessStatus",
    "DAG",
    "DAGNode",
    "DAGNodeInfo",
    "DAGPatch",
    "DAGPatchType",
    "DBTNode",
    "DBTNodeType",
    "EvaluatorAction",
    "ExecutionMode",
    "ExecutionPhase",
    "ExecutionState",
    "ExecutionStatus",
    "ExperienceRecord",
    "NodeErrorCode",
    "NodeResult",
    "NodeResultEvaluation",
    "NodeState",
    "NodeStatus",
    "OrchestratorResult",
    "OrchestratorRunStatus",
    "OutputContract",
    "Plan",
    "ResumeError",
    "ResumeErrorCode",
    "TokenComponent",
    "TokenComponentType",
    "TokenPeriod",
    "TokenStats",
    "TokenUsage",
    "ValidationAction",
    "ValidationIssue",
    "ValidationIssueType",
    "ValidationResult",
    "WorkflowCheckpoint",
    "WorkflowDAG",
    "WorkflowErrorCode",
    "WorkflowProgress",
    "WorkflowResult",
    "WorkflowState",
    "WorkflowSummary",
    "now_iso",
]
