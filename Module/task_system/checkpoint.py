"""CheckpointManager — 任务状态持久化。

每轮 DAG 迭代后保存 TaskState 快照。支持 pause/resume 和崩溃恢复。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.task_system.schema import TaskId, TaskState

logger = logging.getLogger(__name__)


class CheckpointManager:
    """任务状态快照的持久化管理。

    写入策略：write-to-tmp + atomic rename（进程中途崩溃不留下损坏文件）。

    职责：
        - save:  每轮迭代后保存 TaskState
        - load:  resume 时恢复 TaskState
        - delete: 任务终态后清理

    MUTATION 节点的特殊处理：
        执行前写 node.status="executing" + idempotency_key
        崩溃恢复时 node.status="executing" → 标记为 "unrecoverable"，绝不重试
    """

    # TODO: __init__ 接收 checkpoint 目录路径
    # TODO: write-to-tmp + atomic rename 实现
    # TODO: TTL 清理（按文件日期）

    async def save(self, state: TaskState) -> None:
        """保存当前 TaskState 快照。

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("CheckpointManager.save() 尚未实现")

    async def load(self, task_id: TaskId) -> TaskState | None:
        """加载 TaskState 快照。

        Returns:
            TaskState 如果 checkpoint 存在，否则 None

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("CheckpointManager.load() 尚未实现")

    async def delete(self, task_id: TaskId) -> None:
        """删除任务的 checkpoint。

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("CheckpointManager.delete() 尚未实现")
