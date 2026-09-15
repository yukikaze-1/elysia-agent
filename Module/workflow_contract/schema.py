"""WorkflowContract 契约层 — 跨边协议类型。

core 世界与 Module/workflow_engine 世界之间唯一跨边类型来源。
零业务逻辑：只含类型定义、枚举、协议 ABC、无状态容器。
不 import core.*，不 import Module.workflow_engine。

源：Module/workflow_engine/schema/{protocol,types,output}.py + protocol.py（HITLNotifier）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, NewType, TypeVar

from pydantic import BaseModel, Field

from Module.tool.tool_descriptor import Tool  # 循环已解除（workflow_engine 分离 Task 11）：Module.tool 不再 import core
from Module.tool.capability import RBACLevel  # 同上——顶层直接 import，无需底部延迟 import / model_rebuild


# ═══════════════════════════════════════════════════════════════════════════════
# 基础类型 — 标识符别名
# ═══════════════════════════════════════════════════════════════════════════════

WorkflowId = NewType("WorkflowId", str)
"""任务唯一标识符。

NewType 而非 str：防止调用方随意传参（如把 user_input 当 task_id）。
WorkflowEngine 内部生成，对外透明。序列化就是 str，零开销。
"""

AgentId = NewType("AgentId", str)
"""Agent 唯一标识符（Sub-Agent / Worker Agent）。

WorkflowEngine 内部生成，用于 submit_approval 路由到等待审批的 Worker。
"""

ApprovalId = NewType("ApprovalId", str)
"""审批请求唯一标识符。

WorkflowEngine 内部生成，用于 cancel_approval 清理前端收件箱卡片。
"""

SessionId = NewType("SessionId", str)
"""会话唯一标识符。

由 CognitiveLoop 管理，WorkflowEngine 仅用于日志关联和 Debug 追踪。
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

class WorkflowStatus(StrEnum):
    """任务状态机——12 态。

    入口阶段
        QUEUED → PENDING → PLANNING → RUNNING

    执行中分支（从 RUNNING 出发）
        软暂停:   RUNNING → PAUSE_REQUESTED → PAUSED → RESUMING → RUNNING
        取消暂停: PAUSED → CANCELLED（直接取消，无需 resume）
        审批:     RUNNING → WAITING_APPROVAL → RUNNING
                  WAITING_APPROVAL → CANCELLED（等待审批时取消）
        软取消:   RUNNING → CANCEL_REQUESTED → CANCELLED
        完成:     RUNNING → COMPLETED

    早期退出
        QUEUED → CANCELLED（排队中取消）
        PENDING → CANCELLED（submit 后立即取消）
        PLANNING → CANCELLED（规划阶段取消）
        PLANNING → FAILED（规划阶段异常）

    终态: COMPLETED / FAILED / CANCELLED（不可逆）
    内部: REPLAN 在 RUNNING 内部完成（Replanner.replan → apply_patch → 继续 RUNNING），不暴露为独立状态。
    """
    QUEUED = "queued"
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


class SubmitRejectionCode(StrEnum):
    """submit() 同步拒绝码——6 项。

    WorkflowEngine.submit() 参数校验不通过时返回，
    CognitiveLoop 据此生成用户可见的错误信息。
    不会出现在 WorkflowState 或 WorkflowEvent 中。
    """
    NOT_READY = "not_ready"                              # WorkflowEngine 尚未 start()
    INVALID_PARAM = "invalid_param"                     # 参数校验不通过
    PLAN_TOO_LARGE = "plan_too_large"                   # DAG 预估节点数超限
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"  # 上下文窗口不足
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"     # 预估 token 消耗超用户剩余配额
    QUEUE_FULL = "QUEUE_FULL"                           # 执行队列已满，快速失败

    # RBACLevel 已归位 Module/tool/capability.py（canonical home），顶部直接 import，供 HITLData.rbac_level 使用。


# ═══════════════════════════════════════════════════════════════════════════════
# 约束
# ═══════════════════════════════════════════════════════════════════════════════

class ConstraintConfig(BaseModel):
    """全局约束配置（WorkflowEngine 构造时注入）。

    WorkflowEngine 内所有执行路径强制遵守。这些是"天花板"——
    通过 WorkflowConstraints.resolve(global) 合并，任务级 None → 全局值，超限 → clamp。

    字段（**标注的是"实际是否被执行"，不是设计意图**——RC-9 的教训：
    `DAGNode.max_duration_s` 与 6 个 `WorkflowErrorCode` 就因为"声明了当已生效"
    长期无人发现）：

        max_total_tokens:     整个任务 LLM 调用总 token 上限（硬）
        max_context_tokens:   单次 prompt 最大 token 数（**预留：当前无执行点**）。
                              唯一引用是 `core/evaluator.py` 的 TODO 注释；实现前
                              **不得**当作已生效（`make check-contract` 把它登记在
                              `_PENDING_WRITERS`）。
        max_failure_count:    连续失败 N 次后熔断（硬）。
                              当前 v1 中 replan 只能由 failure 触发，故此值
                              同时充当 max_replans。若以后引入非失败驱动的 replan
                              （如"换个更优方案"），需拆为独立字段。
        max_dag_nodes:        DAG 节点数上限（硬）
        max_retries_per_node: 单节点最大重试次数（硬；执行点在
                              `workflow_engine/core/execution.py`，节点级 None 时回退到它）
        max_mutations:        敏感操作次数上限（**软：只作为 Planner/Delegation 的
                              prompt 提示**，执行期不强制）。要变硬约束需在
                              `PlanValidator` 里加校验点——在那之前它拦不住任何东西。
        max_task_duration_s:  整个任务最大执行时间（秒）（硬）
        max_tool_error_chars: 工具错误消息截断长度（软）
    """
    max_total_tokens: int       = Field(default=100_000,  ge=1)   # 至少 1 token，否则无法执行
    # 预留：无执行点（见类 docstring）。保留字段是为了不破坏既有配置文件的解析。
    max_context_tokens: int     = Field(default=32_000,   ge=1)
    max_failure_count: int      = Field(default=5,         ge=1)
    max_dag_nodes: int          = Field(default=20,        ge=1)
    max_retries_per_node: int   = Field(default=3,         ge=0)
    # 软约束：只进 prompt（planner/delegation），执行期不强制（见类 docstring）。
    max_mutations: int          = Field(default=10,        ge=0)
    max_task_duration_s: float  = Field(default=600.0,     ge=0)  # 10 分钟
    max_tool_error_chars: int   = Field(default=2000,      ge=0)


T = TypeVar("T", bound=int | float)
"""_resolve_field 泛型参数——int/float 双字段共用。"""


class WorkflowConstraints(BaseModel):
    """任务级约束（DelegationPreparer 产出）。

    所有字段 None 表示"不覆盖，使用全局默认"。
    调用 resolve(global) 后得到所有字段均已填充的有效约束，
    Planner / PlanValidator 直接使用，不再手写 min/fallback。

    字段：
        max_dag_nodes:       本次任务 DAG 步数上限（LLM 根据意图复杂度估算）
        max_mutations:       本次任务敏感操作次数上限（LLM 根据意图风险等级估算）
        max_total_tokens:    本次任务总 token 上限（None=全局默认，LLM 估算或调用方指定）
        max_task_duration_s: 本次任务最大时长秒数（None=全局默认）
        max_failure_count:   本次任务最大连续失败次数（None=全局默认）
    """
    max_dag_nodes: int | None = Field(default=None, ge=1)
    max_mutations: int | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_task_duration_s: float | None = Field(default=None, ge=0)
    max_failure_count: int | None = Field(default=None, ge=1)

    def resolve(self, global_: ConstraintConfig) -> WorkflowConstraints:
        """用全局约束填充所有 None 字段，返回全量有效约束。

        规则：
            1. None → 取全局值
            2. 非 None 但超过全局 → 以全局为准（clamp），记录 warning
            3. 非 None 且 ≤ 全局 → 保留任务级值

        任务级约束是全局的"子集天花板"——只应更严格，不应更宽松。
        DelegationPreparer 产出超过全局的值是 bug，但不会导致崩溃。

        Args:
            global_: 全局约束配置（天花板）。

        Returns:
            WorkflowConstraints: 全量有效约束（所有字段已填充，不超过全局上限）。
        """
        import logging
        _log = logging.getLogger(__name__)

        def _resolve_field(
            name: str, task_val: T | None, global_val: T | None, clamp_reason: str = "",
        ) -> T | None:
            """解析单个字段：None → global；> global → clamp+warn；≤ global → task。"""
            if task_val is None:
                return global_val
            if global_val is not None and task_val > global_val:
                _log.warning(
                    "WorkflowConstraints.resolve: %s=%s 超过全局上限 %s，clamp 到全局值（%s）",
                    name, task_val, global_val, clamp_reason,
                )
                return global_val
            return task_val

        return WorkflowConstraints(
            max_dag_nodes=_resolve_field(
                "max_dag_nodes", self.max_dag_nodes, global_.max_dag_nodes,
                "DAG 节点数",
            ),
            max_mutations=_resolve_field(
                "max_mutations", self.max_mutations, global_.max_mutations,
                "敏感操作次数",
            ),
            max_total_tokens=_resolve_field(
                "max_total_tokens", self.max_total_tokens, global_.max_total_tokens,
                "总 token",
            ),
            max_task_duration_s=_resolve_field(
                "max_task_duration_s", self.max_task_duration_s, global_.max_task_duration_s,
                "任务时长",
            ),
            max_failure_count=_resolve_field(
                "max_failure_count", self.max_failure_count, global_.max_failure_count,
                "连续失败次数",
            ),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 委派上下文
# ═══════════════════════════════════════════════════════════════════════════════

class ReturnMode(StrEnum):
    """Planner / Evaluator 产出报告模式。"""
    NATURAL_LANGUAGE = "natural_language"  # 自然语言总结（默认）
    STRUCTURED = "structured"              # 结构化数据（node_results）


class DelegationContext(BaseModel):
    """DelegationPreparer 产出的委派沙盒。

    Planner 在这个沙盒内工作——只能看到 DelegationPreparer 筛选过的工具和上下文。

    所有字段由 DelegationPreparer.prepare() 填充，
    传入 WorkflowEngine.submit() 后由 Planner 直接消费。

    字段：
        intent:              LLM 输出的完整任务意图（非用户原始输入）
        hints:               LLM 观察到的元信息（紧急程度、偏好等），可为 None
        tools:              本次任务可用的工具列表（从全量工具中筛选）
        task_constraints:    本次任务级限制（None 字段 = 不覆盖全局约束）
        summarized_context:  LLM 摘要的相关上下文（≤ 500 字）
        session_id:          关联会话 ID，用于日志和 Debug 追踪
        return_mode:         产出报告模式——自然语言总结 或 结构化数据
    """
    intent: str
    hints: str | None = None
    tools: list[Tool] = Field(default_factory=list)
    task_constraints: WorkflowConstraints = Field(default_factory=WorkflowConstraints)
    summarized_context: str = ""
    session_id: SessionId
    return_mode: ReturnMode = ReturnMode.NATURAL_LANGUAGE


class DelegationStatus(StrEnum):
    """DelegationPreparer.prepare() 的三态判定结果。"""
    OK = "ok"                              # 委派上下文准备完成，可 submit
    NO_MATCHING_TOOLS = "no_matching_tools"  # 工具库缺所需工具
    # TODO: v1 不区分"零工具匹配"和"部分工具缺失"，两者都返回 NO_MATCHING_TOOLS。
    #       后续可拆为 NO_TOOLS_AVAILABLE（全缺）和 PARTIAL_TOOLS_MISSING（部分缺）。
    TOO_VAGUE = "too_vague"                # 意图模糊，需用户澄清


class DelegationResult(BaseModel):
    """DelegationPreparer.prepare() 的产出。

    status 决定 context 是否有值：
        ok               → context 有值，调用 WorkflowEngine.submit()
        no_matching_tools → context=None，工具库缺所需工具
        too_vague        → context=None，意图模糊需用户澄清

    字段：
        status:              三态判定结果
        context:             status=ok 时的委派上下文
        reason:              LLM 判定理由（始终有，用于调试）
        missing_tools:      status=no_matching_tools 时，缺少的工具列表
        suggestion:          status=too_vague 时，给用户的澄清建议
    """
    status: DelegationStatus
    context: DelegationContext | None = None
    reason: str = ""
    missing_tools: list[str] = Field(default_factory=list)
    suggestion: str | None = None

    token_usage: dict | None = None
    # LLM 调用消耗: {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
    # status=ok 时始终有值；异常时可能为 None

    # UserProfile 字段由 CognitiveLoop 侧定义，当前占位。
    # session_messages 截断策略后续讨论。


# ═══════════════════════════════════════════════════════════════════════════════
# 提交结果
# ═══════════════════════════════════════════════════════════════════════════════

class SubmitError(BaseModel):
    """submit() 拒绝时的结构化错误。

    字段：
        code:        错误码
        message:     人类可读的错误描述
        details:     额外上下文（如超限值 vs 限制值）
        suggestions: 修复建议列表
    """
    code: SubmitRejectionCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════════
# HITL 审批数据
# ═══════════════════════════════════════════════════════════════════════════════

class HITLData(BaseModel):
    """HITL 审批请求数据（WorkflowEngine → HITLNotifier.request_approval）。

    同一份数据既走 HITLNotifier 快通道（强类型），
    也走 WorkflowMailbox 审计流（WorkflowEvent.data.approval_needed）。

    字段：
        task_id:       所属任务 ID
        approval_id:   审批请求 ID（取消时用于匹配前端卡片）
        agent_id:      发起审批的 Worker ID（回传时路由用）
        node_id:       当前 DAG 节点 ID
        node_task:     当前节点在做什么，来自 DAGNode.description
                       （例："搜索昨天天气数据"），帮助用户理解 MUTATION 的上下文
        tool_name:     即将执行的工具名（例："send_email"）
        params:        工具参数预览（脱敏后）
        rbac_level:    工具权限级别，前端据此渲染审批卡片危险程度
        risk_summary:  风险摘要（操作类型、影响范围、参数预览）
    """
    task_id: WorkflowId
    approval_id: ApprovalId
    agent_id: AgentId
    node_id: NodeId
    node_task: str                       # 当前节点的目标任务描述
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    rbac_level: RBACLevel = RBACLevel.MUTATION
    risk_summary: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# WorkflowMailbox 事件
# ═══════════════════════════════════════════════════════════════════════════════

class WorkflowEventType(StrEnum):
    """WorkflowMailbox 事件类型——7 种。"""
    PROGRESS = "progress"
    APPROVAL_NEEDED = "approval_needed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    QUEUED = "queued"


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

    # progress（进度）: {
    #     "task_id": WorkflowId,
    #     "node_id": NodeId,
    #     "node_description": str,  # 当前节点在做什么
    #     "percent": float,         # 0~1，整个任务的完成比例
    #     "message": str,           # 简短描述
    # }

    # approval_needed（待审批）: {            # 与 HITLData 字段一一对应
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

    # completed（完成）: {
    #     "task_id": WorkflowId,
    #     "result": str,            # 自然语言摘要
    #     "summary": str,           # 简短摘要
    #     "total_nodes": int,
    #     "completed_nodes": int,
    #     "failed_nodes": int,
    # }

    # failed（失败）: {
    #     "task_id": WorkflowId,
    #     "error": str,             # 人类可读错误描述
    #     "error_type": WorkflowErrorCode,  # 任务级错误码
    # }

    # cancelled（取消）: {
    #     "task_id": WorkflowId,
    #     "reason": str,            # 取消原因
    # }

    # paused（暂停）: {
    #     "task_id": WorkflowId,
    #     "message": str,           # 暂停说明
    # }


# ═══════════════════════════════════════════════════════════════════════════════
# HITL 协议 ABC
# ═══════════════════════════════════════════════════════════════════════════════

class HITLNotifier(ABC):
    """审批快速通道协议。

    WorkflowEngine 推审批请求到前端时使用。
    不关心谁在另一端——可能是 EventBus、WebSocket、或未来的任意机制。

    实现者保证：
    - 非阻塞：request_approval / cancel_approval 不应长时间阻塞
    - 不抛异常：异常由实现者自行处理，WorkflowEngine 不感知
    """

    @abstractmethod
    async def request_approval(self, event_data: HITLData) -> None:
        """推送审批请求到前端收件箱。

        审批进入前端收件箱列表，不弹窗打断用户。
        无超时——用户随时可以处理。

        Args:
            event_data: HITLData（Pydantic model），包含:
                - task_id: WorkflowId
                - approval_id: ApprovalId
                - agent_id: AgentId
                - node_id: str
                - node_task: str         # 当前节点的目标任务描述
                - tool_name: str
                - params: dict
                - rbac_level: RBACLevel  # 工具权限级别
                - risk_summary: str      # 操作类型、影响范围、参数预览
        """
        ...

    @abstractmethod
    async def cancel_approval(self, approval_ids: list[ApprovalId]) -> None:
        """批量取消已发出的审批请求。

        用于任务取消（该任务所有 pending approval）、
        Worker 终止、Replan 换方案等场景，
        清理前端收件箱中已不需要的审批卡片。

        Args:
            approval_ids: 要取消的审批请求 ID 列表。空列表无操作。
        """
        ...


# ── Tool / RBACLevel 已在文件顶部直接 import（Task 11 循环解除后无需延迟 import / model_rebuild）──
