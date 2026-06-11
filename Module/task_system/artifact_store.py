"""ArtifactStore — 节点产物存储。

独立于 TaskState——节点产出大对象（搜索结果、文档全文等）存于此，
NodeState 只保存 artifact_refs 引用。

v1: 内存 dict，任务终态时清理。
v2: 文件 / 数据库。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import NodeId

if TYPE_CHECKING:
    from Module.task_system.schema import Artifact, TaskId

logger = logging.getLogger(__name__)


class ArtifactStore:
    """节点产物的类型化存储。

    所有节点产物统一存于此，NodeResult.artifact_refs 持有引用。
    v1 不区分大小——全部进 ArtifactStore。

    Artifact 有 artifact_type（对应 OutputContract.output_type），
    便于按类型检索和校验。

    v1 实现：内存 dict[str, Artifact]。
    """

    # TODO: v2 考虑文件存储（大 artifact）或数据库

    async def put(self, artifact: Artifact) -> str:
        """存入 artifact，返回 artifact_id。

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("ArtifactStore.put() 尚未实现")

    async def get(self, artifact_id: str) -> Artifact | None:
        """按 ID 获取 artifact。

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("ArtifactStore.get() 尚未实现")

    async def get_by_node(self, node_id: NodeId) -> list[Artifact]:
        """获取某个节点产出的所有 artifact。

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("ArtifactStore.get_by_node() 尚未实现")

    async def get_by_task(self, task_id: TaskId) -> list[Artifact]:
        """获取某个任务的所有 artifact。

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("ArtifactStore.get_by_task() 尚未实现")

    async def delete_by_task(self, task_id: TaskId) -> None:
        """清理任务的所有 artifact（终态时调用）。

        Raises:
            NotImplementedError: 骨架阶段
        """
        raise NotImplementedError("ArtifactStore.delete_by_task() 尚未实现")
