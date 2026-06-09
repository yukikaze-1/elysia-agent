"""TaskSystem — 异步 Plan-Execute-Evaluate-Replan 任务编排模块。

独立于 CognitiveLoop，通过 TaskMailbox + HITLNotifier 协议与外界通信。
"""

from Module.task_system.core.orchestrator import Orchestrator
from Module.task_system.core.sub_agent import SubAgent
from Module.task_system.delegation_preparer import DelegationPreparer
from Module.task_system.infra import AgentInfra
from Module.task_system.protocol import HITLNotifier
from Module.task_system.schema import (
    # 基础
    TaskId,
    AgentId,
    ApprovalId,
    SessionId,
    NodeId,
    TaskStatus,
    TaskErrorCode,
    ResumeErrorCode,
    NodeStatus,
    RBACLevel,
    # 第一层：跨层协议
    ConstraintConfig,
    TaskConstraints,
    DelegationContext,
    DelegationResult,
    SubmitError,
    ResumeError,
    HITLData,
    # CognitiveLoop 侧类型（TaskSystem 引用，不拥有）
    UserProfile,
    Tool,
    # 第二层：核心输出
    TaskEvent,
    TaskSummary,
    TaskProgress,
    DAGNodeInfo,
    TaskDAG,
    AgentInfo,
    AgentList,
    TokenComponent,
    TokenUsage,
    TokenStats,
    # 第三层：内部编排
    Plan,
    DAGNode,
    DAGPatch,
    DBTNode,
    DBTNodeType,
    NodeResult,
    TaskResult,
    ExperienceRecord,
    # Plan 校验
    ValidationIssueType,
    ValidationIssue,
    ValidationResult,
    # 节点产物
    Artifact,
    # Orchestrator / Sub-Agent 运行时
    RunStatus,
    OrchestratorResult,
    TaskState,
    NodeState,
)

__all__ = [
    # 协议
    "HITLNotifier",
    "AgentInfra",
    "DelegationPreparer",
    "SubAgent",
    "Orchestrator",
    # 基础
    "TaskId",
    "AgentId",
    "ApprovalId",
    "SessionId",
    "NodeId",
    "TaskStatus",
    "TaskErrorCode",
    "ResumeErrorCode",
    "NodeStatus",
    "RBACLevel",
    # 第一层：跨层协议
    "ConstraintConfig",
    "TaskConstraints",
    "DelegationContext",
    "DelegationResult",
    "SubmitError",
    "ResumeError",
    "HITLData",
    # CognitiveLoop 侧类型（占位）
    "UserProfile",
    "Tool",
    # 第二层：核心输出
    "TaskEvent",
    "TaskSummary",
    "TaskProgress",
    "DAGNodeInfo",
    "TaskDAG",
    "AgentInfo",
    "AgentList",
    "TokenComponent",
    "TokenUsage",
    "TokenStats",
    # 第三层：内部编排
    "Plan",
    "DAGNode",
    "DAGPatch",
    "DBTNode",
    "DBTNodeType",
    "NodeResult",
    "TaskResult",
    "ExperienceRecord",
    # Plan 校验
    "ValidationIssueType",
    "ValidationIssue",
    "ValidationResult",
    # 节点产物
    "Artifact",
    # Orchestrator / Sub-Agent 运行时
    "RunStatus",
    "OrchestratorResult",
    "TaskState",
    "NodeState",
]
