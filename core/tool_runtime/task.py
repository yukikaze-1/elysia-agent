"""QueuedToolCall — 一次待执行的工具调用的数据模型（原 Task）。

ToolCallDispatcher（原 TaskScheduler）Worker Pool 消费这些调用。
认知循环产出工具调用规划时，每个 tool_call 封装为一个 QueuedToolCall 提交到 Worker Pool。

**扩展储备（`P3-01-RESERVED`）**：`QueuedToolCallKind.BACKGROUND`、`depends_on`、`children_ids`、
`run_in_background`、`progress`、`new_background()` 与 checkpoint 序列化
（`to_dict`/`from_dict`）当前均无生产消费者，但 2026-09-12 决策**保留作扩展储备**
（非死代码）——勿按引用审计删除。详见 `docs/review/2026-08-30-后端架构分层与模块化
增量评估.md` §ARCH-INC-P1-01「收窄范围」与 §ARCH-INC-P3-01 处置记录。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, TypedDict

from core.schema.types import RequestId, TaskId
from core.utils import new_task_id


class QueuedToolCallKind(Enum):
    TOOL_CALL = "tool_call"       # AI 规划的工具调用
    BACKGROUND = "background"     # P3-01-RESERVED: 脱离对话流的长时后台任务（预留）


class QueuedToolCallState(Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 终态集合
_TERMINAL_STATES = frozenset({
    QueuedToolCallState.COMPLETED, QueuedToolCallState.FAILED, QueuedToolCallState.CANCELLED,
})


class ToolCallPayload(TypedDict):
    """TOOL_CALL 类型任务的 payload。"""
    tool_name: str
    arguments: dict[str, Any]
    call_id: str


@dataclass
class QueuedToolCall:
    """调度中心的任务单元。

    运行时字段 (_future, _worker_task) 不持久化。
    """

    id: TaskId
    type: QueuedToolCallKind
    status: QueuedToolCallState = QueuedToolCallState.PENDING
    priority: int = 5               # 0=最高, 5=默认, 9=后台
    request_id: RequestId | None = None   # 触发本调用的用户请求 ID（原 parent_id）
    depends_on: list[TaskId] = field(default_factory=list)   # P3-01-RESERVED: DAG 依赖预留
    children_ids: list[TaskId] = field(default_factory=list)  # P3-01-RESERVED: 任务树预留
    payload: ToolCallPayload | dict[str, Any] = field(default_factory=dict)  # dict = BACKGROUND/other
    result: Any = None
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 2
    run_in_background: bool = False  # P3-01-RESERVED: 脱离对话流预留
    progress: float = 0.0            # P3-01-RESERVED: 0~1 进度预留（生产恒 0.0）
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # 运行时字段 — 不序列化
    _future: asyncio.Future | None = field(default=None, repr=False, compare=False)
    _worker_task: asyncio.Task | None = field(default=None, repr=False, compare=False)

    # ---- 属性 ----

    @property
    def is_terminal(self) -> bool:
        """任务是否已处于终态 (COMPLETED / FAILED / CANCELLED)。"""
        return self.status in _TERMINAL_STATES

    # ---- Future ----

    def get_future(self) -> asyncio.Future:
        """获取或创建 Future，dispatch_tool_calls 用它等待结果。"""
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        return self._future

    # ---- 时间戳 ----

    def touch(self) -> None:
        """更新时间戳为当前时间。"""
        self.updated_at = time.time()

    # ---- 重试 ----

    def can_retry(self) -> bool:
        """只有非终态或 FAILED 状态且未达重试上限才能重试。"""
        if self.status == QueuedToolCallState.CANCELLED:
            return False
        if self.status == QueuedToolCallState.COMPLETED:
            return False
        return self.retry_count < self.max_retries

    def retry_backoff_seconds(self) -> float:
        """指数退避: 2^n 秒, cap 30s。"""
        return min(2.0 ** self.retry_count, 30.0)

    # ---- 树操作 ----

    def add_child(self, child_id: TaskId) -> None:
        """添加子任务 ID。

        Args:
            child_id: 子任务 ID
        """
        self.children_ids.append(child_id)

    # ---- 工厂方法 ----

    @classmethod
    def new_tool_call(
        cls,
        request_id: RequestId | None,
        tool_name: str,
        arguments: dict[str, Any],
        call_id: str = "",
        priority: int = 5,
    ) -> "QueuedToolCall":
        """创建工具调用子任务。

        Args:
            request_id: 触发本调用的用户请求 ID
            tool_name: 工具名称
            arguments: 工具参数
            call_id: LLM tool_call_id（D6-07：sourceCallId 数据源，thread 到 worker ctx.call_id）
            priority: 优先级，0=最高, 5=默认

        Returns:
            QueuedToolCall: 新创建的工具调用任务
        """
        payload: ToolCallPayload = {
            "tool_name": tool_name,
            "arguments": arguments,
            "call_id": call_id,
        }
        return cls(  # type: ignore[reportCallIssue]
            id=new_task_id(),
            type=QueuedToolCallKind.TOOL_CALL,
            request_id=request_id,
            priority=priority,
            payload=payload,
        )

    @classmethod
    def new_background(
        cls,
        payload: dict[str, Any],
        priority: int = 9,
    ) -> "QueuedToolCall":
        """创建后台任务（P3-01-RESERVED：无生产调用方，保留作扩展储备）。

        Args:
            payload: 任务负载数据
            priority: 优先级，默认 9（后台）

        Returns:
            QueuedToolCall: 新创建的后台任务
        """
        return cls(  # type: ignore[reportCallIssue]
            id=new_task_id(),
            type=QueuedToolCallKind.BACKGROUND,
            priority=priority,
            run_in_background=True,
            max_retries=3,
            payload=payload,
        )

    # ---- 序列化 (checkpoint) — P3-01-RESERVED：无生产持久化闭环，保留作扩展储备 ----

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典，用于 checkpoint 持久化。

        Returns:
            dict[str, Any]: 任务的字典表示（不含运行时字段）
        """
        return {
            "id": self.id,
            "type": self.type.value,
            "status": self.status.value,
            "priority": self.priority,
            "request_id": self.request_id,
            "depends_on": self.depends_on,
            "children_ids": self.children_ids,
            "payload": self.payload,
            "error": self.error,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "run_in_background": self.run_in_background,
            "progress": self.progress,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QueuedToolCall":
        """从字典反序列化，用于 checkpoint 恢复。

        Args:
            d: 任务的字典表示

        Returns:
            QueuedToolCall: 反序列化的任务实例
        """
        return cls(  # type: ignore[reportCallIssue]
            id=d["id"],
            type=QueuedToolCallKind(d["type"]),
            status=QueuedToolCallState(d.get("status", "pending")),
            priority=d.get("priority", 5),
            # 兼容旧 checkpoint 键（改名迁移：parent_id → request_id）
            request_id=d.get("request_id", d.get("parent_id")),
            depends_on=d.get("depends_on", []),
            children_ids=d.get("children_ids", []),
            payload=d.get("payload", {}),
            error=d.get("error"),
            retry_count=d.get("retry_count", 0),
            max_retries=d.get("max_retries", 2),
            run_in_background=d.get("run_in_background", False),
            progress=d.get("progress", 0.0),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
        )
