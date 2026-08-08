"""Task — AI 规划任务的统一数据模型。

TaskScheduler Worker Pool 消费这些 task。
认知循环产出工具调用规划时，每个 tool_call 封装为一个 Task 提交到 Worker Pool。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, TypedDict

from core.schema.types import TaskId
from core.utils import new_task_id


class TaskType(Enum):
    TOOL_CALL = "tool_call"       # AI 规划的工具调用
    BACKGROUND = "background"     # 脱离对话流的长时后台任务 (预留)


class TaskStatus(Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 终态集合
_TERMINAL_STATES = frozenset({
    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
})


class ToolCallPayload(TypedDict):
    """TOOL_CALL 类型任务的 payload。"""
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class Task:
    """调度中心的任务单元。

    运行时字段 (_future, _worker_task) 不持久化。
    """

    id: TaskId
    type: TaskType
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 5               # 0=最高, 5=默认, 9=后台
    parent_id: str | None = None    # 实收 RequestId，本批不裁决（TaskId 贯通范围外）
    depends_on: list[TaskId] = field(default_factory=list)   # 预留: DAG 依赖
    children_ids: list[TaskId] = field(default_factory=list)
    payload: ToolCallPayload | dict[str, Any] = field(default_factory=dict)  # dict = BACKGROUND/other
    result: Any = None
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 2
    run_in_background: bool = False  # 预留: 脱离对话流
    progress: float = 0.0            # 预留: 0~1 进度
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
        if self.status == TaskStatus.CANCELLED:
            return False
        if self.status == TaskStatus.COMPLETED:
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
        parent_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        priority: int = 5,
    ) -> "Task":
        """创建工具调用子任务。

        Args:
            parent_id: 父任务 ID
            tool_name: 工具名称
            arguments: 工具参数
            priority: 优先级，0=最高, 5=默认

        Returns:
            Task: 新创建的工具调用任务
        """
        payload: ToolCallPayload = {"tool_name": tool_name, "arguments": arguments}
        return cls(  # type: ignore[reportCallIssue]
            id=new_task_id(),
            type=TaskType.TOOL_CALL,
            parent_id=parent_id,
            priority=priority,
            payload=payload,
        )

    @classmethod
    def new_background(
        cls,
        payload: dict[str, Any],
        priority: int = 9,
    ) -> "Task":
        """创建后台任务。

        Args:
            payload: 任务负载数据
            priority: 优先级，默认 9（后台）

        Returns:
            Task: 新创建的后台任务
        """
        return cls(  # type: ignore[reportCallIssue]
            id=new_task_id(),
            type=TaskType.BACKGROUND,
            priority=priority,
            run_in_background=True,
            max_retries=3,
            payload=payload,
        )

    # ---- 序列化 (checkpoint) ----

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
            "parent_id": self.parent_id,
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
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        """从字典反序列化，用于 checkpoint 恢复。

        Args:
            d: 任务的字典表示

        Returns:
            Task: 反序列化的任务实例
        """
        return cls(  # type: ignore[reportCallIssue]
            id=d["id"],
            type=TaskType(d["type"]),
            status=TaskStatus(d.get("status", "pending")),
            priority=d.get("priority", 5),
            parent_id=d.get("parent_id"),
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
