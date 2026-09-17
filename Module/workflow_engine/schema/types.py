"""WorkflowEngine 基础类型——工具函数、NewType 别名、枚举。

所有其他 schema 子模块的底层依赖。零 schema 内部依赖。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, NewType

from pydantic import BeforeValidator


# ── 时间戳工具 ────────────────────────────────────────────────────────

def now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。

    WorkflowEngine 内所有 timestamp 字段统一使用此函数生成，
    替代各模块内重复的 _now() / datetime.now(timezone.utc).isoformat() 样板代码。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_utc(v: object) -> str:
    """Pydantic BeforeValidator——归一化 ISO 8601 字符串到 UTC。

    接受 datetime 或 str，输出 UTC ISO 8601 字符串（Z 后缀）。
    空字符串直通（未设置的时间戳）。
    naive datetime（无 tzinfo）视为 UTC。

    Args:
        v: datetime 对象或 ISO 8601 字符串。

    Returns:
        str: UTC ISO 8601 格式字符串（Z 后缀）。

    Raises:
        ValueError: 无法解析输入值。
    """
    if isinstance(v, str) and v == "":
        return v
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, str):
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raise ValueError(f"无法解析为 IsoTimestamp: {v!r}")


# Pydantic 注解类型——声明为 Annotated[str, BeforeValidator(...)]，
# Pydantic 在 model_dump/model_validate 时自动归一化。
IsoTimestamp = Annotated[str, BeforeValidator(_normalize_utc)]


# ── 基础类型 ────────────────────────────────────────────────────────────────────

# 跨边标识符别名（WorkflowId/AgentId/ApprovalId/SessionId/NodeId）已随契约层提取
# 迁至 Module/workflow_contract/schema.py——消费方直接从契约层 import（__init__ 已不重导出）。

ArtifactId = NewType("ArtifactId", str)
"""产物唯一标识符。

ArtifactStore 存储时的 key，与 DAGNode.output_as 对应。
NewType 防止调用方把 node_id / task_id 误当 artifact_id 传入。
"""


# 跨边基础枚举（WorkflowStatus / SubmitRejectionCode）已随契约层提取迁至
# Module/workflow_contract/schema.py；RBACLevel 归位 Module/tool/capability.py（Task 8）。
# 消费方直接从契约层 import（__init__ 已不重导出）。

# ── 基础枚举（引擎内部） ───────────────────────────────────────────────────────

class WorkflowErrorCode(StrEnum):
    """任务运行时错误码——10 项。

    WorkflowState.error_code 使用，checkpoint 持久化，
    WorkflowEvent(type="failed") 的 error_type 字段。
    调用方（CognitiveLoop）据此生成用户可见的错误信息。

    按发生阶段分组：
        规划阶段:    PLANNING_FAILED, PLAN_TOO_LARGE
        执行阶段:    TOOL_ERROR, PERMISSION_DENIED, TOKEN_BUDGET_EXCEEDED
        资源耗尽:    FAILURE_MELTDOWN, TIMEOUT, MUTATION_LIMIT_REACHED
        外部:        CANCELLED_BY_USER
        兜底:        INTERNAL_ERROR
    """
    PLANNING_FAILED = "planning_failed"                # Planner LLM 调用失败，无法生成 Plan
    PLAN_TOO_LARGE = "plan_too_large"                  # Planner 生成的 DAG 节点数超限
    TOOL_ERROR = "tool_error"                          # 工具执行错误（任务级）
    PERMISSION_DENIED = "permission_denied"            # 审批被拒（任务级）
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"    # 任务总 token 消耗达上限
    FAILURE_MELTDOWN = "failure_meltdown"              # 失败次数达上限，禁止 Replan
    TIMEOUT = "timeout"                                 # 任务整体超时
    MUTATION_LIMIT_REACHED = "mutation_limit_reached"  # 敏感操作次数达上限
    CANCELLED_BY_USER = "cancelled_by_user"            # 用户主动取消
    INTERNAL_ERROR = "internal_error"                  # 内部异常（unexpected 兜底）


class ResumeErrorCode(StrEnum):
    """resume() 失败时的错误码。

    调用方（CognitiveLoop）据此生成用户可见的错误信息。
    """
    CHECKPOINT_NOT_FOUND = "checkpoint_not_found"            # checkpoint 文件不存在
    CHECKPOINT_CORRUPTED = "checkpoint_corrupted"            # checkpoint 数据损坏
    CHECKPOINT_VERSION_MISMATCH = "checkpoint_version_mismatch"  # schema 版本不兼容
    INVALID_STATE = "invalid_state"                          # 非 PAUSED 状态 / CAS 冲突
    PENDING_APPROVALS_FAILED = "pending_approvals_failed"    # 重推审批请求失败
    NODE_RECOVERY_FAILED = "node_recovery_failed"            # 已完成节点恢复失败
    QUEUE_FULL = "QUEUE_FULL"                                # resume 队列满，快速失败


class NodeStatus(StrEnum):
    """DAG 节点的运行时状态——8 态。

    Orchestrator 主循环中 NodeState 使用，DAGNodeInfo（查询视图）复用。

    正常流程
        PENDING → RUNNING → SUCCESS

    重试
        RUNNING → FAILED → PENDING → RUNNING（达到上限 → UNRECOVERABLE）

    审批阻断
        RUNNING → WAITING_APPROVAL → RUNNING（审批通过）
        WAITING_APPROVAL → FAILED（审批拒绝）

    任务取消（批量标记）
        PENDING / RUNNING / WAITING_APPROVAL → CANCELLED

    跳过
        PENDING → SKIPPED（可选节点在 replan 后被移除）
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNRECOVERABLE = "unrecoverable"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLED = "cancelled"


class NodeErrorCode(StrEnum):
    """节点执行错误码——NodeResult.error 使用。

    只覆盖 Executor 层（单节点工具调用）的失败原因。
    任务级错误（PLANNING_FAILED、FAILURE_MELTDOWN 等）在 WorkflowErrorCode 中。
    """
    TOOL_ERROR = "tool_error"              # 工具返回错误（runtime error）
    TOOL_TIMEOUT = "tool_timeout"          # 工具调用超时（区别于 ExecutionStatus.TIMEOUT 的语义）
    TOOL_NOT_FOUND = "tool_not_found"      # Executor 无法匹配到合适的工具
    PERMISSION_DENIED = "permission_denied"  # 用户拒绝审批
    WORKER_CRASH = "worker_crash"          # Worker Agent 异常退出
    CONTEXT_OVERFLOW = "context_overflow"  # 单节点 prompt 超上下文窗口
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"  # 单节点 LLM 调用 token 超限
    NETWORK_ERROR = "network_error"        # 瞬态网络故障（可重试）
    # S2-D7：节点声明的 `input_bindings` 一条都解析不出来（上游没跑 / 漏边 /
    # 引用了被全量重建丢弃的节点 / output key 拼错）。原实现只 warning 后把空输入
    # 塞进 prompt，LLM 会自行编造内容、Evaluator 还可能判成功——**静默错误结果**。
    MISSING_INPUT = "missing_input"
