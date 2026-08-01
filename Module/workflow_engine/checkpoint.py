"""CheckpointManager — 任务状态持久化。

每轮 DAG 迭代后保存 WorkflowCheckpoint（WorkflowState + ExecutionState）快照。
支持 pause/resume 和崩溃恢复。

v1: 内存 dict，进程重启后丢失。
v2: 文件持久化（write-to-tmp + atomic rename）。

接口边界：
    外部 → WorkflowCheckpoint（强类型 Pydantic 模型）
    内部 → 直接存储 WorkflowCheckpoint 对象（v1 内存，零开销）
    v2 文件持久化时在文件 I/O 边界做序列化/反序列化。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.workflow_contract import WorkflowId
    from Module.workflow_engine.schema import WorkflowCheckpoint

logger = logging.getLogger(__name__)


class CheckpointManager:
    """任务状态快照的持久化管理。

    v1 实现：内存 dict[WorkflowId, WorkflowCheckpoint]（直接存储 Pydantic 对象）。
    每轮 Orchestrator 迭代后保存，resume 时加载，终态后清理。

    接口对外暴露 WorkflowCheckpoint（强类型 Pydantic 模型），内部直接存储对象——
    v1 无序列化开销。v2 文件持久化时在文件读写边界做 model_dump / model_validate。

    写入策略（v2）：
        write-to-tmp + atomic rename（进程中途崩溃不留下损坏文件）。

    MUTATION 节点的特殊处理（v2）：
        执行前写 node.status="executing" + idempotency_key
        崩溃恢复时 node.status="executing" → 标记为 "unrecoverable"，绝不重试
    """

    def __init__(self) -> None:
        """Initialize the checkpoint manager with empty in-memory store."""
        self._store: dict[str, WorkflowCheckpoint] = {}

    # ── 持久化 ──────────────────────────────────────────────────────────

    async def save(self, task_id: WorkflowId, checkpoint: WorkflowCheckpoint) -> None:
        """保存 WorkflowCheckpoint 快照。

        v1 直接存储 Pydantic 对象（内存，零序列化开销）。
        v2 在此基础上增加文件写入（model_dump → 文件）。

        Args:
            task_id: 任务的唯一标识。
            checkpoint: 任务状态快照（WorkflowState + ExecutionState）。
        """
        self._store[str(task_id)] = checkpoint
        logger.debug("CheckpointManager.save: task_id=%s", task_id)

    async def load(self, task_id: WorkflowId) -> WorkflowCheckpoint | None:
        """加载 checkpoint 快照。

        v1 直接返回内存中的 Pydantic 对象。
        v2 从文件读取 + model_validate 恢复。

        Args:
            task_id: 任务的唯一标识。

        Returns:
            WorkflowCheckpoint | None: 快照对象，不存在返回 None。
        """
        cp = self._store.get(str(task_id))
        if cp is not None:
            logger.debug("CheckpointManager.load: task_id=%s", task_id)
        return cp

    async def delete(self, task_id: WorkflowId) -> None:
        """删除任务的 checkpoint。

        Args:
            task_id: 任务的唯一标识。
        """
        existed = self._store.pop(str(task_id), None) is not None
        if existed:
            logger.debug("CheckpointManager.delete: task_id=%s", task_id)
