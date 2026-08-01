"""ArtifactStore — 节点产物存储。

独立于 TaskState——节点产出大对象（搜索结果、文档全文等）存于此，
NodeState.artifact_refs 只保存引用。

v1: 内存 dict，任务终态时清理。
v2: 文件 / 数据库。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.workflow_engine.schema import ArtifactId, NodeId

if TYPE_CHECKING:
    from Module.workflow_engine.schema import Artifact, TaskId

logger = logging.getLogger(__name__)


class ArtifactStore:
    """节点产物的类型化存储。

    所有节点产物统一存于此，NodeResult.artifact_refs 持有 ArtifactId 引用。
    v1 不区分大小——全部进内存 dict。

    提供三个维度的查询：
        - 按 artifact_id 精确取
        - 按 node_id 查某个节点的所有产物
        - 按 task_id 查某个任务的所有产物

    v1 实现：dict[str, Artifact] + 两个索引 dict（set 语义）。
    v2 可换文件存储 / 数据库，接口不变。
    """

    def __init__(self) -> None:
        """Initialize the artifact store with empty internal storage and indexes."""
        self._artifacts: dict[ArtifactId, Artifact] = {}
        self._by_node: dict[NodeId, set[ArtifactId]] = {}   # NodeId → {ArtifactId}
        self._by_task: dict[TaskId, set[ArtifactId]] = {}   # TaskId → {ArtifactId}

    # ── 写入 ──────────────────────────────────────────────────────────────

    async def put(self, artifact: Artifact) -> None:
        """存入 artifact。

        artifact_id 由调用方（Executor）在创建 Artifact 时填入，
        来源是 Planner 声明的 DAGNode.output_as。ArtifactStore 不生成 ID，
        只负责存储和索引。

        幂等——同 ID 重复写入是覆盖。

        Args:
            artifact: Artifact 对象，需已设置 artifact_id、node_id、task_id。
        """
        aid = artifact.artifact_id
        nid = artifact.node_id
        tid = artifact.task_id

        self._artifacts[aid] = artifact
        self._by_node.setdefault(nid, set()).add(aid)
        self._by_task.setdefault(tid, set()).add(aid)

        logger.debug("ArtifactStore.put: id=%s, type=%s, node=%s, task=%s",
                     aid, artifact.artifact_type, nid, tid)

    # ── 查询 ──────────────────────────────────────────────────────────────

    async def get(self, artifact_id: ArtifactId) -> Artifact | None:
        """按 ID 获取 artifact。

        Args:
            artifact_id: Artifact 的唯一标识。

        Returns:
            Artifact | None: 匹配的 artifact，不存在返回 None。
        """
        return self._artifacts.get(artifact_id)

    async def get_by_node(self, node_id: NodeId) -> list[Artifact]:
        """获取某个节点产出的所有 artifact。

        Args:
            node_id: 节点的唯一标识。

        Returns:
            list[Artifact]: 该节点产出的所有 artifact。无结果返回空列表。
        """
        ids = self._by_node.get(node_id, set())
        artifacts: list[Artifact] = []
        for aid in ids:
            a = self._artifacts.get(aid)
            if a is not None:
                artifacts.append(a)
            else:
                logger.warning(
                    "ArtifactStore: 索引不一致——_by_node[%s] 含 %s 但 _artifacts 中不存在",
                    node_id, aid,
                )
        return artifacts

    async def get_by_task(self, task_id: TaskId) -> list[Artifact]:
        """获取某个任务的所有 artifact。

        Args:
            task_id: 任务的唯一标识。

        Returns:
            list[Artifact]: 该任务的所有 artifact。无结果返回空列表。
        """
        ids = self._by_task.get(task_id, set())
        artifacts: list[Artifact] = []
        for aid in ids:
            a = self._artifacts.get(aid)
            if a is not None:
                artifacts.append(a)
            else:
                logger.warning(
                    "ArtifactStore: 索引不一致——_by_task[%s] 含 %s 但 _artifacts 中不存在",
                    task_id, aid,
                )
        return artifacts

    # ── 清理 ──────────────────────────────────────────────────────────────
    # TODO: v2 增加 delete_by_node(node_id)——DAGPatch 删除节点时需清理对应 artifact，
    #       当前 Replan stub 不会触发此场景

    async def delete_by_task(self, task_id: TaskId) -> None:
        """清理任务的所有 artifact（终态时调用）。

        同时清理主存储和 node/task 两个索引。
        即使 _artifacts 中部分数据已缺失（如崩溃恢复），也能正确清理索引残留。

        Args:
            task_id: 任务的唯一标识。
        """
        ids = self._by_task.pop(task_id, set())
        if not ids:
            return

        # 清理主存储（先收集 node_id，确保 node 索引清理不依赖 _artifacts）
        aid_to_nid: dict[ArtifactId, NodeId] = {}
        for aid in ids:
            artifact = self._artifacts.pop(aid, None)
            if artifact is not None:
                aid_to_nid[aid] = artifact.node_id
            else:
                logger.warning(
                    "ArtifactStore: _by_task[%s] 含 %s 但 _artifacts 中不存在，"
                    "尝试从 _by_node 推断 node_id",
                    task_id, aid,
                )
                # 从 _by_node 反查 node_id（扫描所有 node，适用于少量残留）
                # TODO: v2 artifact 数量大时，可维护 aid→nid 反向索引以 O(1) 查找
                for nid, node_ids in self._by_node.items():
                    if aid in node_ids:
                        aid_to_nid[aid] = nid
                        break

        # 清理 node 索引
        for aid, nid in aid_to_nid.items():
            node_ids = self._by_node.get(nid)
            if node_ids is not None:
                node_ids.discard(aid)
                if not node_ids:
                    del self._by_node[nid]

        logger.debug("ArtifactStore.delete_by_task: task=%s, count=%d", task_id, len(ids))
