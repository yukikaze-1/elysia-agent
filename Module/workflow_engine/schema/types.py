"""TaskSystem 基础类型——工具函数、NewType 别名、枚举。

所有其他 schema 子模块的底层依赖。零 schema 内部依赖。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, NewType

from pydantic import BaseModel, BeforeValidator, Field


# ── 时间戳工具 ────────────────────────────────────────────────────────

def now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。

    TaskSystem 内所有 timestamp 字段统一使用此函数生成，
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


def parse_iso(ts: str) -> datetime:
    """将 IsoTimestamp 字符串解析为 offset-aware UTC datetime。

    替代各处重复的 try/except + fromisoformat + replace(tzinfo=...) 样板代码。

    Args:
        ts: ISO 8601 时间戳字符串。空字符串返回 epoch。

    Returns:
        datetime: offset-aware UTC datetime，空字符串返回 1970-01-01 UTC。
    """
    if not ts:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Pydantic 注解类型——声明为 Annotated[str, BeforeValidator(...)]，
# Pydantic 在 model_dump/model_validate 时自动归一化。
IsoTimestamp = Annotated[str, BeforeValidator(_normalize_utc)]


# ── 基础类型 ────────────────────────────────────────────────────────────────────

TaskId = NewType("TaskId", str)
"""任务唯一标识符。

NewType 而非 str：防止调用方随意传参（如把 user_input 当 task_id）。
TaskSystem 内部生成，对外透明。序列化就是 str，零开销。
"""

AgentId = NewType("AgentId", str)
"""Agent 唯一标识符（Sub-Agent / Worker Agent）。

TaskSystem 内部生成，用于 submit_approval 路由到等待审批的 Worker。
"""

ApprovalId = NewType("ApprovalId", str)
"""审批请求唯一标识符。

TaskSystem 内部生成，用于 cancel_approval 清理前端收件箱卡片。
"""

SessionId = NewType("SessionId", str)
"""会话唯一标识符。

由 CognitiveLoop 管理，TaskSystem 仅用于日志关联和 Debug 追踪。
NewType 防止调用方把 task_id / user_input 误当 session_id 传入。
"""

NodeId = NewType("NodeId", str)
"""DAG / DBT 节点唯一标识符。

Planner 生成 DAGNode 时分配，Executor 生成 DBTNode 时沿用或派生。
NewType 防止调用方把 task_id / artifact_id 误当 node_id 传入。
"""

ArtifactId = NewType("ArtifactId", str)
"""产物唯一标识符。

ArtifactStore 存储时的 key，与 DAGNode.output_as 对应。
NewType 防止调用方把 node_id / task_id 误当 artifact_id 传入。
"""


# ═══════════════════════════════════════════════════════════════════════════════
# 基础枚举
# ═══════════════════════════════════════════════════════════════════════════════

class TaskStatus(StrEnum):
    """任务状态机——11 态。

    入口阶段
        PENDING → PLANNING → RUNNING

    执行中分支（从 RUNNING 出发）
        软暂停:   RUNNING → PAUSE_REQUESTED → PAUSED → RESUMING → RUNNING
        取消暂停: PAUSED → CANCELLED（直接取消，无需 resume）
        审批:     RUNNING → WAITING_APPROVAL → RUNNING
                  WAITING_APPROVAL → CANCELLED（等待审批时取消）
        软取消:   RUNNING → CANCEL_REQUESTED → CANCELLED
        完成:     RUNNING → COMPLETED

    早期退出
        PENDING → CANCELLED（submit 后立即取消）
        PLANNING → CANCELLED（规划阶段取消）
        PLANNING → FAILED（规划阶段异常）

    终态: COMPLETED / FAILED / CANCELLED（不可逆）
    内部: REPLAN 在 RUNNING 内部完成（Replanner.replan → apply_patch → 继续 RUNNING），不暴露为独立状态。
    """
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
    """submit() 同步拒绝码——5 项。

    TaskSystem.submit() 参数校验不通过时返回，
    CognitiveLoop 据此生成用户可见的错误信息。
    不会出现在 TaskState 或 TaskEvent 中。
    """
    NOT_READY = "not_ready"                              # TaskSystem 尚未 start()
    INVALID_PARAM = "invalid_param"                     # 参数校验不通过
    PLAN_TOO_LARGE = "plan_too_large"                   # DAG 预估节点数超限
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"  # 上下文窗口不足
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"     # 预估 token 消耗超用户剩余配额


class TaskErrorCode(StrEnum):
    """任务运行时错误码——10 项。

    TaskState.error_code 使用，checkpoint 持久化，
    TaskEvent(type="failed") 的 error_type 字段。
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
    任务级错误（PLANNING_FAILED、FAILURE_MELTDOWN 等）在 TaskErrorCode 中。
    """
    TOOL_ERROR = "tool_error"              # 工具返回错误（runtime error）
    TOOL_TIMEOUT = "tool_timeout"          # 工具调用超时（区别于 ExecutionStatus.TIMEOUT 的语义）
    TOOL_NOT_FOUND = "tool_not_found"      # Executor 无法匹配到合适的工具
    PERMISSION_DENIED = "permission_denied"  # 用户拒绝审批
    WORKER_CRASH = "worker_crash"          # Worker Agent 异常退出
    CONTEXT_OVERFLOW = "context_overflow"  # 单节点 prompt 超上下文窗口
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"  # 单节点 LLM 调用 token 超限
    NETWORK_ERROR = "network_error"        # 瞬态网络故障（可重试）


class RBACLevel(StrEnum):
    """工具权限级别。注册时声明，运行时校验。

    TODO: 归属待定。Tools 在 Module/tool/ 声明权限级别，TaskSystem 执行时校验。
    两个模块都需要此枚举。后续确定 canonical home（建议 Module/tool/），
    TaskSystem 侧改为 re-export。
    """
    READ_ONLY = "read_only"       # 只读，完全自主
    SAFE_WRITE = "safe_write"     # 安全写入（沙箱内），条件自主
    MUTATION = "mutation"         # 外部变更，必须审批
