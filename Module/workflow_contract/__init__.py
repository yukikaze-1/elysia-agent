"""WorkflowContract — 跨边契约层。

core 世界与 Module/workflow_engine 世界之间唯一跨边类型来源。
零业务逻辑：只含类型定义、枚举、协议 ABC、无状态容器。
不 import core.*，不 import Module.workflow_engine。

消费者：
  - core 侧：cognitive loop/tools、agent lifecycle、input server/debug_routes。
  - workflow_engine 侧：引擎本体。
"""

from Module.workflow_contract.schema import (
    AgentId,
    ApprovalId,
    ConstraintConfig,
    DelegationContext,
    DelegationResult,
    DelegationStatus,
    HITLData,
    HITLNotifier,
    NodeId,
    ReturnMode,
    SessionId,
    SubmitError,
    SubmitRejectionCode,
    WorkflowConstraints,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowId,
    WorkflowStatus,
)
from Module.workflow_contract.infra import AgentInfra
from Module.workflow_contract.utils import (
    format_tools,
    parse_iso,
    strip_markdown_fence,
)

__all__ = [
    # 跨边运行时注入容器
    "AgentInfra",
    # 纯函数工具
    "format_tools",
    "parse_iso",
    "strip_markdown_fence",
    # 标识符别名
    "AgentId",
    "ApprovalId",
    "NodeId",
    "SessionId",
    "WorkflowId",
    # 基础枚举
    "SubmitRejectionCode",
    "WorkflowStatus",
    # 约束
    "ConstraintConfig",
    "WorkflowConstraints",
    # 委派协议
    "DelegationContext",
    "DelegationResult",
    "DelegationStatus",
    "ReturnMode",
    "SubmitError",
    # HITL 协议
    "HITLData",
    "HITLNotifier",
    # 事件
    "WorkflowEvent",
    "WorkflowEventType",
]
