"""第三层：内部编排 —— Planner / Executor / Evaluator 数据模型。

注意：部分类型（TaskResult, NodeResultEvaluation, ExecutionStatus 等）
通过 TaskEvent.data 和查询 API 暴露给外部消费者，是稳定公共契约的一部分。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .types import (
    ArtifactId,
    IsoTimestamp,
    NodeErrorCode,   # ★ 从 types.py 导入（原在 internal 区域）
    NodeId,
    NodeStatus,
    RBACLevel,
    SessionId,
    TaskErrorCode,
    TaskId,
    TaskStatus,
    now_iso,
)
from .protocol import Tool
from .output import TaskEventType, TokenComponentType, TokenComponent


# ═══════════════════════════════════════════════════════════════════════════════
# 第三层：内部编排 —— Planner / Executor / Evaluator 数据模型
# ═══════════════════════════════════════════════════════════════════════════════
# ↓ 下面这些字段待敲定，先留骨架 ↓


# ── Orchestrator 运行状态 ─────────────────────────────────────────────────────

class OrchestratorRunStatus(StrEnum):
    """Orchestrator.run() 的返回状态——6 态。

    Orchestrator 驱动 Plan 直到完成、遇到障碍、或被外部中断。
    非 SUCCESS 状态由 Sub-Agent 做决策。

    内部出向
        SUCCESS       — 所有 required 节点完成
        FAILED        — 不可恢复（资源耗尽、内部异常）
        NEEDS_REPLAN  — 结构性失败，DAG 走不通，需要修改 Plan
        BLOCKED       — 等待用户审批回传
    外部中断
        PAUSED        — 收到暂停请求，已保存 checkpoint，可恢复
        CANCELED      — 收到取消请求，已保存 checkpoint（终态，不可恢复）
    """
    SUCCESS = "success"
    FAILED = "failed"
    NEEDS_REPLAN = "needs_replan"
    BLOCKED = "blocked"
    PAUSED = "paused"
    CANCELED = "canceled"


class OrchestratorResult(BaseModel):
    """Orchestrator.run() 的返回值。

    SUCCESS 时 checkpoint_state 有最终快照。
    NEEDS_REPLAN 时 error_context 包含失败节点和诊断信息。
    BLOCKED 时等待外部审批回传。
    PAUSED 时保存了 checkpoint，TaskSystem 可调用 resume() 恢复。
    """
    status: OrchestratorRunStatus
    error_context: dict[str, Any] = Field(default_factory=dict)
    # ↑ NEEDS_REPLAN / FAILED 时包含:
    #     {"reason": str, "failed_nodes": list[NodeId]}
    #     特殊情况（死锁、内部异常）只有 reason，无 failed_nodes
    checkpoint_state: TaskCheckpoint | None = None
    # ↑ 当前 TaskState + ExecutionState 快照。Sub-Agent 用于:
    #     PAUSED       → CheckpointManager.save(checkpoint_state)，等 resume 后恢复
    #     BLOCKED      → 等审批回传后恢复执行
    #     NEEDS_REPLAN → checkpoint_state.exec.node_states 传给 Replanner.replan()
    #     死锁 / ABORT 时为 None（快照无意义）


# ── Orchestrator 状态追踪 ─────────────────────────────────────────────────────────

class NodeState(BaseModel):
    """Orchestrator 内部：单个节点的运行时状态。

    与 DAGNode（声明式）和 NodeResult/NodeResultEvaluation（执行/评估产出）不同——
    NodeState 是 Orchestrator 主循环中追踪"这个节点现在什么状态"的记录。

    对应的是 DAGNode（非 DBTNode）。v1 不做 DBT，单工具直接调用。
    """
    node_id: NodeId
    status: NodeStatus = NodeStatus.PENDING
    attempt: int = 0                       # 已尝试次数（0=尚未首次执行）
    started_at: IsoTimestamp = Field(default_factory=now_iso)
    finished_at: IsoTimestamp = Field(default_factory=now_iso)
    artifact_refs: list[ArtifactId] = Field(default_factory=list)
    # ↑ ArtifactStore 中该节点完成后的产物引用列表。
    #   从 NodeResult.artifact_refs 复制而来（_apply_evaluation 的 continue 分支）。
    #   NodeResult 存"本次执行产生"，NodeState 存"节点最终关联"（checkpoint 用）。
    #   所有产物统一存 ArtifactStore，引用即 artifact_id。
    #   v1: 内存 dict，v2: 文件 / 数据库。
    summary: str = ""                      # 节点完成后的简短摘要
    error: str | None = None               # 人类可读的错误诊断（Evaluator 产出的 reason），
    #                                          retry / replan / abort 时写入。
    error_code: NodeErrorCode | None = None  # 机器可读错误码（来自 NodeResult.error），
    #                                          retry / replan / abort 时写入。
    #                                          ExperienceRecord / Planner 做结构化匹配用。
    evaluation: NodeResultEvaluation | None = None  # Evaluator 完整判定（含 execution_status、
    #                                                  business_status、suggested_action），
    #                                                  Replanner.replan() 做诊断决策用。


class TaskState(BaseModel):
    """SubAgent 管理：任务级运行时状态。

    SubAgent 创建并控制生命周期。Orchestrator 运行时读取 status/plan，
    写入 status（终态转换），但不拥有 TaskState。

    checkpoint 时与 ExecutionState 合并持久化。
    """
    task_id: TaskId
    status: TaskStatus = TaskStatus.PENDING
    plan: Plan | None = None                   # 当前生效的 Plan（Replan 后更新）
    tools: list[Tool] = Field(default_factory=list)  # 本次任务可用的工具列表（从 DelegationContext 透传，Executor 消费）
    session_id: SessionId = Field(
        default_factory=lambda: SessionId("")
    )  # 关联会话 ID（从 DelegationContext 透传，保持 NewType 类型安全）
    created_at: IsoTimestamp = Field(default_factory=now_iso)                       # ISO 8601
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
    error: str | None = None                   # 人类可读的错误原因（终态 FAILED 时写入）
    error_code: TaskErrorCode | None = None    # 机器可读的任务级错误码（终态 FAILED 时写入）
    token_entries: list[TokenComponent] = Field(default_factory=list)  # TokenLedger write-through 逐条记录
    token_usage: dict[str, int] = Field(default_factory=dict)  # 按组件汇总 {"planner": N, "executor": N, ...}

    def is_terminal(self) -> bool:
        """任务是否已到达终态。"""
        return self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )


class ExecutionPhase(StrEnum):
    """Orchestrator 主循环的执行阶段。

    比 TaskStatus 更细粒度——TaskStatus.RUNNING 内部有三个阶段轮转。
    """
    EXECUTING = "executing"
    EVALUATING = "evaluating"
    FINALIZING = "finalizing"


class ExecutionState(BaseModel):
    """Orchestrator 内部：Plan 执行层的运行时状态。

    Orchestrator 主循环中唯一可变的状态对象。
    每次循环迭代后更新，与 TaskState 合并后 checkpoint 持久化。

    SubAgent 不读 node_states 内容——它只负责在 checkpoint/resume 时传递。
    """
    node_states: dict[NodeId, NodeState] = Field(default_factory=dict)
    current_phase: ExecutionPhase = ExecutionPhase.EXECUTING
    waiting_for_approval: bool = False         # 等待审批回传
    pause_requested: bool = False          # 外部请求暂停（Orchestrator 检查后返回 PAUSED）
    cancel_requested: bool = False         # 外部请求取消（Orchestrator 检查后返回 CANCELED）
    created_at: IsoTimestamp = Field(default_factory=now_iso)                   # Orchestrator._init_exec_state() 时赋值
    updated_at: IsoTimestamp = Field(default_factory=now_iso)


# ── Checkpoint ──────────────────────────────────────────────────────────────────


class TaskCheckpoint(BaseModel):
    """任务执行快照——TaskState + ExecutionState 的组合容器。

    Orchestrator._build_checkpoint() 产出，CheckpointManager 持久化，
    SubAgent 在 resume / NEEDS_REPLAN / PAUSED 路径消费。

    两个字段均为 Pydantic 模型实例（非 dict）——checkpoint 在内存中保持强类型，
    CheckpointManager 在 save/load 边界做 model_dump / model_validate 转换。
    """
    task: TaskState
    exec: ExecutionState


# ── 节点产出契约 ──────────────────────────────────────────────────────────────

class OutputContract(BaseModel):
    """节点产出的结构化契约。

    Planner 在 DAGNode 上声明，Evaluator 用来判定 business_status。
    当前 v1 两字段紧凑实现，够用且边界清晰。

    TODO: 后续版本考虑拆分为四字段，覆盖从"产出什么"到"怎么校验"的完整链路：
        expected_outcome: str          — 人类可读说明（替代当前 description）
        output_contract: dict | None   — 机器可校验 JSON Schema（当前 json_schema）
        success_criteria: str          — 质量要求/门槛（LLM 语义评判，如"温度误差 ±2°C"）
        validation_rules: list[dict]   — 可执行校验规则（如 [{"path":"$.temp","op":"gte","value":-100}]）
        v1 暂不实施。JSON Schema 本身可延伸（minimum/maximum/pattern/enum），不会堵死路。

        output_type 为 v2 类型注册表预留——v1 留空；非空时走 OutputTypeRegistry 查预定义
        schema + 特化校验器。一条字段，零额外复杂度。

    例（v1 当前实现）：
        {
            "description": "返回北京未来3天天气，包含温度、湿度、降水概率",
            "json_schema": {
                "type": "object",
                "properties": {
                    "temperature": {"type": "number"},
                    "humidity": {"type": "number"},
                    "precipitation": {"type": "number"},
                },
                "required": ["temperature"],
            },
            "output_type": "",
        }
    """
    description: str = ""                           # 自然语言期望（LLM Evaluator 语义比对）
    json_schema: dict[str, Any] | None = None      # JSON Schema（程序化校验，可选）
    output_type: str = ""                          # v2 类型注册表 key，v1 留空


# ── DAG 与行为树 ────────────────────────────────────────────────────────────────

class ExecutionMode(StrEnum):
    """DAG 节点的执行模式——Planner 显式标注，Executor 按标注执行。

    tool_call:  正常 function-calling 匹配工具并执行（默认，向后兼容）
    llm_direct: 跳过工具匹配，直接调 LLM 生成文本（汇总/编译/改写/整理类节点）
    """
    TOOL_CALL = "tool_call"
    LLM_DIRECT = "llm_direct"


class DAGNode(BaseModel):
    """Planner 产出的单个 DAG 节点。

    只声明目标和期望结果，不包含执行策略——DBT 由 Executor 运行时生成。
    图结构（依赖关系）提升到 DAG.edges，节点不持有。
    数据流通过 input_bindings 声明（正交于 edges 执行顺序）。

    字段：
        node_id:         节点唯一标识
        description:     做什么（给 Executor 看的任务描述）
        output_contract: 期望产出什么（给 Evaluator 做 business_status 判定）
        required:        是否必须成功（False → 失败可跳过，不阻塞后续节点）
        input_bindings:  从 ArtifactStore 取的产物 ID 列表（数据依赖，正交于 edges 执行依赖）
        output_as:       产出物存在 ArtifactStore 中的 ArtifactId
        execution_mode:  执行模式——tool_call（默认，匹配工具调用）或 llm_direct（直接调 LLM）
        max_retries:     per-node 最大重试次数（None → 使用全局 ConstraintConfig.max_retries_per_node）
        max_duration_s:  per-node 最大执行时长秒数（None → 无限制，与 ConstraintConfig.max_task_duration_s 命名对齐）
    """
    node_id: NodeId
    description: str = ""                         # 节点目标描述
    output_contract: OutputContract = Field(default_factory=OutputContract)  # 期望产出 + 约束
    required: bool = True                         # 是否必须成功（False → 可跳过）
    input_bindings: list[ArtifactId] = Field(default_factory=list)   # 从 ArtifactStore 取哪些产物
    output_as: ArtifactId | None = None            # 产出存到 ArtifactStore 的 ID（None=无产物）
    execution_mode: ExecutionMode = ExecutionMode.TOOL_CALL  # 默认 tool_call，向后兼容
    max_retries: int | None = None                # None=使用全局默认
    max_duration_s: float | None = None           # None=无限制


class DAG(BaseModel):
    """有向无环图。

    edges 方向: (from, to) → from 必须先完成，to 才能开始。
    等价于 from 是 to 的前置依赖。
    """
    nodes: list[DAGNode] = Field(default_factory=list)
    edges: list[tuple[NodeId, NodeId]] = Field(default_factory=list)


class Plan(BaseModel):
    """Planner 产出的 DAG 骨架。

    纯数据结构。dag 持有完整的图信息（nodes + edges）。
    metadata（task_id）归 TaskState，委派上下文由 SubAgent 持有。

    字段：
        dag:          完整的 DAG（nodes + edges）
        plan_version: Replan 时递增
        created_at:   Planner.plan() 首次产出时赋值（ISO 8601）
        updated_at:   每次 Replan 后更新（ISO 8601）
    """
    dag: DAG = Field(default_factory=DAG)
    plan_version: int = 1                  # Replan 时递增
    created_at: IsoTimestamp = Field(default_factory=now_iso)                   # Planner 首次产出时赋值
    updated_at: IsoTimestamp = Field(default_factory=now_iso)                   # 每次 Replan 后更新


class DAGPatchType(StrEnum):
    """DAG 补丁类型——3 种。

    Replanner.replan() 根据失败诊断选择合适的补丁策略。
    注意：full_replan 不属于 DAGPatch 职责——
    全量重建由 Sub-Agent 层直接创建新 Plan + 重建 PlanGraph。
    """
    REPLACE_NODES = "replace_nodes"  # 替换指定节点（replace_nodes 字段）
    ADD_NODES = "add_nodes"          # 仅新增节点（add_nodes 字段）
    REMOVE_NODES = "remove_nodes"    # 仅移除节点（remove_nodes 字段）


class DAGPatch(BaseModel):
    """Replan 产出的 DAG 增量修改。

    局部修改——不动已完成节点，只改失败/未执行的部分。
    五种操作按 apply_patch 顺序：replace → remove nodes → add nodes → remove edges → add edges。

    full_replan 不属于 DAGPatch 职责——
    全量重建由 Sub-Agent 层直接创建新 Plan + 重建 PlanGraph。

    字段：
        patch_type:     修改类型（元数据，primary intent）
        reason:         为什么需要修改（来自 Evaluator 诊断）
        trigger_nodes:  哪些失败节点触发了这次 Replan（并行执行时可能多个）
        created_at:     Replanner.replan() 产出时赋值（ISO 8601）
        replace_nodes:  要替换的节点映射，(old_id, new_node)
        remove_nodes:   要移除的 node_id 列表（不包含 replace 中的 old_id）
        add_nodes:      要新增的节点列表（不包含 replace 中的 new_node）
        remove_edges:   要移除的边
        add_edges:      要新增的边
    """
    patch_type: DAGPatchType = DAGPatchType.REPLACE_NODES
    reason: str = ""
    trigger_nodes: list[NodeId] = Field(default_factory=list)
    created_at: IsoTimestamp = Field(default_factory=now_iso)
    replace_nodes: list[tuple[NodeId, DAGNode]] = Field(default_factory=list)
    remove_nodes: list[NodeId] = Field(default_factory=list)
    add_nodes: list[DAGNode] = Field(default_factory=list)
    remove_edges: list[tuple[NodeId, NodeId]] = Field(default_factory=list)
    add_edges: list[tuple[NodeId, NodeId]] = Field(default_factory=list)


# ── Plan 校验 ──────────────────────────────────────────────────────────────────

class ValidationIssueType(StrEnum):
    """Plan 校验问题类型。"""
    CYCLE = "cycle"                              # DAG 有环
    DUPLICATE_ID = "duplicate_id"                # node_id 重复
    MISSING_DEPENDENCY = "missing_dependency"    # edges 引用了不存在的节点
    UNKNOWN_TOOL = "unknown_tool"                # 工具名不在可用工具列表中
    INVALID_INPUT = "invalid_input"              # 节点 input 不满足工具 schema
    MISSING_CONTRACT = "missing_contract"        # output_contract 缺失或非法
    ORPHAN_NODE = "orphan_node"                  # 孤立节点（无依赖且无被依赖）
    UNREACHABLE_NODE = "unreachable_node"        # 不可达节点
    MISSING_CONFIRMATION = "missing_confirmation"  # 高风险节点无确认标记
    MAX_NODES_EXCEEDED = "max_nodes_exceeded"    # 节点数超限
    MAX_DEPTH_EXCEEDED = "max_depth_exceeded"    # DAG 深度超限


class ValidationIssue(BaseModel):
    """Plan 校验发现的单个问题。"""
    type: ValidationIssueType
    node_id: NodeId | None = None
    message: str = ""


class ValidationAction(StrEnum):
    """Plan 校验后的行动。"""
    PROCEED = "proceed"    # 校验通过，继续执行
    FIX_PLAN = "fix_plan"  # 有问题但可修复，重新规划
    ABORT = "abort"        # 不可修复，中止任务


class ValidationResult(BaseModel):
    """Plan 校验结果。"""
    valid: bool = False
    issues: list[ValidationIssue] = Field(default_factory=list)
    action: ValidationAction = ValidationAction.PROCEED


# ── 节点产物 ──────────────────────────────────────────────────────────────────

class Artifact(BaseModel):
    """节点产出的结构化数据，存于 ArtifactStore。

    所有产物统一走 ArtifactStore，NodeResult.artifact_refs 持有引用。
    artifact_id 与 DAGNode.output_as 对应——上游产出时以此 key 存入，
    下游通过 input_bindings 以此 key 取出。
    """
    artifact_id: ArtifactId
    artifact_type: str                         # 产物类型。v1 来自 tool_result.result_type（如 "text"），
                                                # v2 对齐 OutputContract.output_type 类型注册表 key
    node_id: NodeId
    task_id: TaskId
    content: Any = None                        # 实际产物数据——dict、list、str、bytes 等不做限制
    metadata: dict[str, Any] = Field(default_factory=dict)  # 附加信息：大小、格式、来源等
    created_at: IsoTimestamp = Field(default_factory=now_iso)                       # ISO 8601


class DBTNodeType(StrEnum):
    """DBT 节点类型。

    ⚠️ 实验性质。有降级路径：如果 DBT 表现不佳，
    降级为 NodeConfig(max_retries + fallback_tool + on_exhausted)。
    """
    SEQUENCE = "sequence"       # 顺序执行子节点直到全部成功
    SELECTOR = "selector"       # 尝试子节点直到一个成功
    PARALLEL = "parallel"       # 并发执行所有子节点
    ACTION = "action"           # 叶子节点：调用工具
    CONDITION = "condition"     # 分支判断


class DBTNode(BaseModel):
    """行为树节点。

    ⚠️ 实验性质。v1 可先走 LLM 生成，后续视效果决定是否保留或降级。
    """
    node_id: NodeId
    node_type: DBTNodeType
    tool_name: str | None = None         # ACTION 节点专用
    tool_params: dict[str, Any] = Field(default_factory=dict)  # ACTION 节点专用
    condition: str | None = None         # CONDITION 节点专用
    children: list[DBTNode] = Field(default_factory=list)
    max_retries: int = 1
    max_duration_s: float | None = None


# ── 节点执行/评估结果 ────────────────────────────────────────────────────────────────

class ExecutionStatus(StrEnum):
    """Executor 工具执行状态——5 态（v1 活跃 4 态）。

    工具是否正常返回？Orchestrator 据此决定 retry / skip / abort 等路由。

    CANCELLED 为 v2 预留——Orchestrator 可能在任务级取消时直接传 CANCELLED
    到 Evaluator._derive_action()。当前 v1 的 cancel 路径在 Orchestrator 循环
    顶直接返回 CANCELED，绕过 Evaluator。
    """
    OK = "ok"                            # 工具正常返回
    TIMEOUT = "timeout"                  # 工具调用超时
    ERROR = "error"                      # 工具返回错误
    PERMISSION_DENIED = "permission_denied"  # 用户拒绝审批
    CANCELLED = "cancelled"              # ⚠️ TODO(v2): 任务级取消路径预留


class BusinessStatus(StrEnum):
    """Evaluator 业务结果判定——3 态。

    Plan 中声明的期望结果是否达成？
    """
    GOAL_ACHIEVED = "goal_achieved"      # 完全达成
    GOAL_PARTIAL = "goal_partial"        # 部分达成
    GOAL_FAILED = "goal_failed"          # 未达成


class EvaluatorAction(StrEnum):
    """Evaluator 建议的下一步动作——6 态。

    Orchestrator._apply_evaluation() 据此决定 NodeStatus 转换和执行流。
    并非所有 action 在所有上下文中都合法（如 skip 仅用于 required=False 的节点）。
    """
    CONTINUE = "continue"       # 节点成功，继续下一个
    RETRY = "retry"             # 重新入队（attempt+1 后重试）
    SKIP = "skip"               # 跳过（仅非必需节点，标记 SKIPPED 后继续）
    REPLAN = "replan"           # 结构性失败，触发 NEEDS_REPLAN
    ASK_APPROVAL = "ask_approval"  # 需要用户审批，触发 BLOCKED
    ABORT = "abort"             # 不可恢复，触发 FAILED


class NodeResult(BaseModel):
    """Executor 产出——工具执行的原始结果。

    v1 走单工具直接调用（不做 DBT）。所有产物统一存 ArtifactStore，
    NodeResult 只持有引用，不内联数据。

    Evaluator 不修改 NodeResult，而是产出 NodeResultEvaluation 包裹它。
    """
    node_id: NodeId
    artifact_refs: list[ArtifactId] = Field(default_factory=list)  # ArtifactStore 引用（含所有产物）
    error: NodeErrorCode | None = None                       # 节点级执行错误码
    attempt: int = 0                                        # 当前是第几次执行尝试。Executor 始终返回 0（不追踪重试），
                                                            # 实际值由 Orchestrator 在 _execute_nodes 中用 NodeState.attempt 覆盖。
    duration_ms: int = 0                                    # 本次工具调用耗时（毫秒）。Executor 填写，ExperienceRecord 消费


class NodeResultEvaluation(BaseModel):
    """Evaluator 产出——对 NodeResult 的双重判定。

    不修改 NodeResult。Evaluator 读 DAGNode.output_contract 和 ArtifactStore 中
    的实际产物，得出两个维度的判定，包裹原始 NodeResult 一起返回。
    """
    node_id: NodeId
    execution_status: ExecutionStatus = ExecutionStatus.OK   # 工具是否正常返回？（5 态）
    business_status: BusinessStatus = BusinessStatus.GOAL_ACHIEVED  # Plan 期望是否达成？（3 态）
    reason: str = ""                       # 判定依据（为什么判这个结果）
    suggested_action: EvaluatorAction = EvaluatorAction.CONTINUE  # Orchestrator 执行的下一步
    result: NodeResult                     # 被评定的原始执行结果


class TaskResult(BaseModel):
    """任务完成后的结构化结果。

    包含所有节点结果 + 任务级摘要。
    CognitiveLoop 根据 return_mode 决定用自然语言还是结构化数据向用户汇报。

    错误分工：
        任务级  → error (str) + error_code (TaskErrorCode)，来自 TaskState
        节点级  → node_results[node_id].reason + result.error，来自 NodeResultEvaluation
    """
    task_id: TaskId
    status: TaskStatus
    node_results: dict[NodeId, NodeResultEvaluation] = Field(default_factory=dict)
    summary: str = ""                           # 自然语言摘要
    created_at: IsoTimestamp = Field(default_factory=now_iso)                        # TaskState.created_at（ISO 8601）
    completed_at: str = ""                      # 终态时间（ISO 8601），duration = completed_at - created_at
    token_usage: dict[str, int] = Field(        # {"planner": N, "executor": N, "evaluator": N, ...}
        default_factory=dict
    )
    tools_used: list[str] = Field(default_factory=list)   # 审计：本任务调用了哪些工具名
    # 任务级错误（仅在 failed/cancelled 时有值）
    error: str | None = None                    # 人类可读，来自 TaskState.error
    error_code: TaskErrorCode | None = None     # 机器可读，来自 TaskState.error_code


# ── 经验记忆 ────────────────────────────────────────────────────────────────────

class ExperienceRecord(BaseModel):
    """单条任务经验记忆。

    存于 TaskSystem 专属 Milvus namespace，与用户 LTM 独立。

    ⚠️ v1 不做——经验记忆（ExperienceRecord / Reflector / Milvus 写入）
    属于渐进式学习闭环，v1 骨架阶段仅保留类型定义占位。
    字段和 Milvus schema 待 v2 敲定。
    """
    tool_name: str
    error_pattern: str
    params_snapshot: dict[str, Any] = Field(default_factory=dict)
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    diagnosis: str = ""
    fix: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    verified_count: int = 0
    success_after_apply: float = 0.0
    status: Literal["unverified", "verified", "deprecated"] = "unverified"
    occurrences: int = 0
    last_seen: str = ""                  # ISO 8601
