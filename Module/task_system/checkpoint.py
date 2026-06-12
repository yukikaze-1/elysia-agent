"""CheckpointManager — 任务状态持久化。

每轮 DAG 迭代后保存 TaskState + ExecutionState 快照。
支持 pause/resume 和崩溃恢复。

v1: 内存 dict，进程重启后丢失。
v2: 文件持久化（write-to-tmp + atomic rename）。
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from Module.task_system.schema import TaskId

logger = logging.getLogger(__name__)


class CheckpointManager:
    """任务状态快照的持久化管理。

    v1 实现：内存 dict[TaskId, checkpoint]。
    每轮 Orchestrator 迭代后保存，resume 时加载，终态后清理。

    写入策略（v2）：
        write-to-tmp + atomic rename（进程中途崩溃不留下损坏文件）。

    MUTATION 节点的特殊处理（v2）：
        执行前写 node.status="executing" + idempotency_key
        崩溃恢复时 node.status="executing" → 标记为 "unrecoverable"，绝不重试
    """

    def __init__(self) -> None:
        self._store: dict[TaskId, dict[str, Any]] = {}

    # ── 持久化 ──────────────────────────────────────────────────────────

    async def save(self, task_id: TaskId, checkpoint: dict[str, Any]) -> None:
        """保存 checkpoint 快照。

        checkpoint 结构：{"task": TaskState.model_dump(), "exec": ExecutionState.model_dump()}
        """
        self._store[task_id] = checkpoint
        logger.debug("CheckpointManager.save: task_id=%s", task_id)

    async def load(self, task_id: TaskId) -> dict[str, Any] | None:
        """加载 checkpoint 快照。不存在返回 None。"""
        cp = self._store.get(task_id)
        if cp is not None:
            logger.debug("CheckpointManager.load: task_id=%s", task_id)
        return cp

    async def delete(self, task_id: TaskId) -> None:
        """删除任务的 checkpoint。"""
        existed = self._store.pop(task_id, None) is not None
        if existed:
            logger.debug("CheckpointManager.delete: task_id=%s", task_id)
