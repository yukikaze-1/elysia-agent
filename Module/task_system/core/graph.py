"""PlanGraph —— NetworkX DiGraph 封装层。

Plan 保持 Pydantic 模型（序列化 / checkpoint / API），
PlanGraph 提供图算法——环检测、拓扑排序、ready nodes、patch。

Plan ←→ PlanGraph:
    PlanGraph.from_plan(plan)  ← 从 Pydantic 构建 DiGraph
    PlanGraph.to_plan()        → 导出当前状态为 Plan
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import networkx as nx

from Module.task_system.schema import (
    DAGNode,
    DAGNodeInfo,
    DAGPatch,
    NodeId,
    NodeState,
    NodeStatus,
    TaskDAG,
    ValidationIssue,
    ValidationIssueType,
)

if TYPE_CHECKING:
    from Module.task_system.schema import Plan

logger = logging.getLogger(__name__)


class PlanGraph:
    """NetworkX DiGraph 封装——Plan 的图算法层。

    edges 的方向: dep → node（dep 必须先完成）。
    例: A → B 表示 B 依赖 A，A 完成后 B 才能执行。
    """

    def __init__(self, g: nx.DiGraph, nodes: dict[NodeId, DAGNode]) -> None:
        """初始化 PlanGraph。

        Args:
            g: NetworkX 有向图，节点为 NodeId。
            nodes: NodeId 到 DAGNode 的映射，保存节点的完整元数据。
        """
        self._g = g
        self._nodes = nodes  # NodeId → DAGNode（含 description, output_contract, required 等）

    # ── 构造 / 导出 ──────────────────────────────────────────────────────────

    @classmethod
    def from_plan(cls, plan: Plan) -> PlanGraph:
        """从 Pydantic Plan 构建 PlanGraph。

        plan.dag 持有 nodes + edges，直接建图，无需遍历 node.depends_on。

        Args:
            plan: Pydantic Plan 模型，包含 dag.nodes 和 dag.edges。

        Returns:
            构建好的 PlanGraph 实例。
        """
        g = nx.DiGraph()
        nodes: dict[NodeId, DAGNode] = {}

        for node in plan.dag.nodes:
            if node.node_id in nodes:
                raise ValueError(
                    f"PlanGraph.from_plan: 重复 node_id={node.node_id}——"
                    "Planner 产出了两个同 ID 节点。每个节点必须有唯一 ID，"
                    "请检查 Planner prompt 或 DAG 生成逻辑。"
                )
            g.add_node(node.node_id)
            nodes[node.node_id] = node

        for from_id, to_id in plan.dag.edges:
            if from_id not in g.nodes or to_id not in g.nodes:
                logger.warning("PlanGraph: edge 端点不存在 (%s → %s)", from_id, to_id)
                continue
            g.add_edge(from_id, to_id)

        return cls(g, nodes)

    def to_plan(self, plan: Plan | None = None) -> Plan:
        """将当前图状态写回 Plan（更新 dag.nodes + dag.edges）。

        若 plan 为 None，创建新 Plan。调用方通常传入已有 Plan 实例。

        Args:
            plan: 要更新的 Plan 实例。为 None 时创建新 Plan。

        Returns:
            更新后的 Plan 实例。
        """
        from Module.task_system.schema import DAG, Plan
        if plan is None:
            plan = Plan()
        plan.dag = DAG(
            nodes=list(self._nodes.values()),
            edges=[(u, v) for u, v in self._g.edges],
        )
        return plan

    @property
    def node_count(self) -> int:
        """图中节点的数量。"""
        return len(self._g.nodes)

    @property
    def nodes(self) -> dict[NodeId, DAGNode]:
        """NodeId 到 DAGNode 的映射字典。"""
        return self._nodes

    # ── 调度 ─────────────────────────────────────────────────────────────────

    def get_ready_nodes(
        self, node_states: dict[NodeId, NodeState],
    ) -> list[NodeId]:
        """所有前置节点已完成（SUCCESS 或 SKIPPED）且自身 PENDING 的节点。

        SKIPPED 视为完成——可选节点被跳过时不应阻塞下游。
        数据依赖（input_bindings）正交于执行顺序（edges），
        Executor 会容错处理缺失的 artifact。

        遍历 DAG 中每个节点，检查其所有前驱（predecessors）的状态。
        时间复杂度 O(n+e)。

        Args:
            node_states: NodeId 到 NodeState 的映射，记录每个节点的当前执行状态。

        Returns:
            所有就绪节点的 NodeId 列表。
        """
        ready: list[NodeId] = []
        for node_id in self._g.nodes:
            ns = node_states.get(node_id)
            if ns is None or ns.status != NodeStatus.PENDING:
                continue

            # 所有前置节点必须已完成（SUCCESS 或 SKIPPED）
            preds_ok = all(
                node_states.get(p) is not None
                and node_states[p].status in (NodeStatus.SUCCESS, NodeStatus.SKIPPED)
                for p in self._g.predecessors(node_id)
            )
            if preds_ok:
                ready.append(node_id)

        return ready

    def topological_order(self) -> list[NodeId]:
        """拓扑排序——Executor 的调度顺序。

        Returns:
            按拓扑顺序排列的 NodeId 列表。
        """
        return list(nx.topological_sort(self._g))

    def is_terminal(self, node_states: dict[NodeId, NodeState]) -> bool:
        """所有 required=True 的节点都是 SUCCESS 状态。

        Args:
            node_states: NodeId 到 NodeState 的映射，记录每个节点的当前执行状态。

        Returns:
            如果所有必需节点都已成功完成则返回 True，否则返回 False。
        """
        for node_id, node in self._nodes.items():
            if not node.required:
                continue
            ns = node_states.get(node_id)
            if ns is None or ns.status != NodeStatus.SUCCESS:
                return False
        return True

    def has_failed_required(self, node_states: dict[NodeId, NodeState]) -> bool:
        """是否有 required=True 的节点处于 FAILED 或 UNRECOVERABLE。

        Args:
            node_states: NodeId 到 NodeState 的映射，记录每个节点的当前执行状态。

        Returns:
            如果有必需节点处于失败或不可恢复状态则返回 True，否则返回 False。
        """
        for node_id, node in self._nodes.items():
            if not node.required:
                continue
            ns = node_states.get(node_id)
            if ns is not None and ns.status in (NodeStatus.FAILED, NodeStatus.UNRECOVERABLE):
                return True
        return False

    def collect_failure_info(self, node_states: dict[NodeId, NodeState]) -> dict:
        """收集失败节点的诊断信息，供 Sub-Agent 传给 Replanner.replan()。

        Args:
            node_states: NodeId 到 NodeState 的映射，记录每个节点的当前执行状态。

        Returns:
            包含 failed_nodes（失败节点 ID 列表）、reason（失败原因字符串）和
            error_codes（节点 ID 到错误码的映射）的字典。
        """
        failed: list[str] = []
        reasons: list[str] = []
        error_codes: dict[str, str] = {}
        for node_id, ns in node_states.items():
            if ns.status in (NodeStatus.FAILED, NodeStatus.UNRECOVERABLE):
                failed.append(node_id)
                if ns.error_code:
                    error_codes[node_id] = ns.error_code.value
                if ns.error:
                    reasons.append(f"{node_id}: {ns.error}")
        return {
            "failed_nodes": failed,
            "reason": "; ".join(reasons) if reasons else "节点执行失败",
            "error_codes": error_codes,
        }

    # ── 结构校验 ─────────────────────────────────────────────────────────────

    def validate_structure(self) -> list[ValidationIssue]:
        """DAG 结构合法性检查（使用 NetworkX 算法）。

        覆盖以下图结构项：
            1. DAG 是否有环
            2. 依赖引用是否存在
            3. 孤立节点
            4. 不可达节点

        注意：不检查工具名——v1 的 DAGNode 没有 tool_name 字段，
        工具匹配由 Executor 运行时完成。待 v2 补充。

        Returns:
            ValidationIssue 列表，每个元素描述一个结构问题。无问题时返回空列表。
        """
        issues: list[ValidationIssue] = []

        # 1. 环检测
        if not nx.is_directed_acyclic_graph(self._g):
            try:
                cycle = nx.find_cycle(self._g, orientation="original")
                cycle_str = " → ".join(str(u) for u, v, _ in cycle)
                issues.append(ValidationIssue(
                    type=ValidationIssueType.CYCLE,
                    message=f"DAG 有环: {cycle_str}",
                ))
            except nx.NetworkXNoCycle:
                pass

        # 2. 依赖引用检查
        for node_id in self._g.nodes:
            for pred in self._g.predecessors(node_id):
                if pred not in self._g.nodes:
                    issues.append(ValidationIssue(
                        type=ValidationIssueType.MISSING_DEPENDENCY,
                        node_id=node_id,
                        message=f"依赖的节点 {pred} 不存在",
                    ))
        # 3. 孤立节点（>1 节点时为异常，单节点任务正常）
        if len(self._g.nodes) > 1:
            for node_id in self._g.nodes:
                if self._g.in_degree(node_id) == 0 and self._g.out_degree(node_id) == 0:
                    issues.append(ValidationIssue(
                        type=ValidationIssueType.ORPHAN_NODE,
                        node_id=node_id,
                        message="孤立节点——无依赖且无被依赖",
                    ))

        # 4. 不可达节点（没有从根节点出发的路径）
        roots = [n for n in self._g.nodes if self._g.in_degree(n) == 0]
        reachable: set[NodeId] = set(roots)
        for root in roots:
            reachable.update(nx.descendants(self._g, root))
        unreachable = set(self._g.nodes) - reachable
        for node_id in unreachable:
            issues.append(ValidationIssue(
                type=ValidationIssueType.UNREACHABLE_NODE,
                node_id=node_id,
                message="不可达节点——未从任何根节点出发可达",
            ))

        return issues

    # ── DAGPatch ─────────────────────────────────────────────────────────────

    def apply_patch(self, patch: DAGPatch) -> None:
        """应用 DAGPatch 到当前图——五种操作，按序执行。

        Args:
            patch: DAGPatch 实例，包含 replace_nodes、remove_nodes、add_nodes、
                remove_edges、add_edges 五种操作。

        顺序保证了依赖一致性：
            1. replace_nodes — 替换节点（同 ID 仅更新元数据，异 ID 自动重连边）
            2. remove_nodes  — 移除不需要的节点
            3. add_nodes     — 新增节点
            4. remove_edges  — 移除不需要的边
            5. add_edges     — 新增边

        替换节点的边处理：
            - 同 ID 替换：仅更新 DAGNode 元数据，边保持不变（最常见场景）
            - 异 ID 替换：自动将旧节点的入边/出边重连到新节点；
              后续 remove_edges/add_edges 可以覆盖自动重连的结果。

        full_replan 不在此处理——由 Sub-Agent 层创建新 Plan + 重建 PlanGraph。
        """
        # 1. 替换节点
        for old_id, new_node in patch.replace_nodes:
            new_id = new_node.node_id
            if old_id in self._g.nodes:
                if old_id == new_id:
                    # 同 ID 替换——仅更新节点元数据，保留所有边
                    self._nodes[new_id] = new_node
                else:
                    # 异 ID 替换——保存旧边，删旧加新，自动重连
                    in_edges = list(self._g.predecessors(old_id))
                    out_edges = list(self._g.successors(old_id))
                    self._g.remove_node(old_id)
                    self._nodes.pop(old_id, None)
                    self._g.add_node(new_id)
                    self._nodes[new_id] = new_node
                    # 自动重连：旧入边 → 新节点，新节点 → 旧出边
                    for pred in in_edges:
                        self._g.add_edge(pred, new_id)
                    for succ in out_edges:
                        self._g.add_edge(new_id, succ)
            else:
                # 被替换节点不存在（可能已被之前的操作删除）——直接添加
                self._g.add_node(new_id)
                self._nodes[new_id] = new_node

        # 2. 移除节点
        for node_id in patch.remove_nodes:
            if node_id in self._g.nodes:
                self._g.remove_node(node_id)
                self._nodes.pop(node_id, None)

        # 3. 新增节点
        for node in patch.add_nodes:
            self._g.add_node(node.node_id)
            self._nodes[node.node_id] = node

        # 4. 移除边（可以覆盖自动重连的边）
        for from_id, to_id in patch.remove_edges:
            if self._g.has_edge(from_id, to_id):
                self._g.remove_edge(from_id, to_id)

        # 5. 新增边（可以覆盖 remove_edges 的结果）
        for from_id, to_id in patch.add_edges:
            if from_id in self._g.nodes and to_id in self._g.nodes:
                self._g.add_edge(from_id, to_id)

    # ── 查询视图 ─────────────────────────────────────────────────────────────

    def to_dag_view(
        self, node_states: dict[NodeId, NodeState],
    ) -> TaskDAG:
        """构建 Debug WebUI 用的 DAG 视图。

        Args:
            node_states: NodeId 到 NodeState 的映射，记录每个节点的当前执行状态。

        Returns:
            包含节点信息（DAGNodeInfo）和边列表的 TaskDAG 视图。
        """
        dag_nodes: list[DAGNodeInfo] = []
        for node_id in self._g.nodes:
            ns = node_states.get(node_id)
            st = ns.status if ns else NodeStatus.PENDING
            dag_nodes.append(DAGNodeInfo(
                node_id=node_id,
                description=self._nodes[node_id].description,
                status=st,
                progress=1.0 if st == NodeStatus.SUCCESS else 0.0,
                error=ns.error if ns else None,
                error_code=ns.error_code if ns else None,
            ))

        edges: list[tuple[NodeId, NodeId]] = [
            (u, v) for u, v in self._g.edges
        ]

        return TaskDAG(nodes=dag_nodes, edges=edges)

    def compute_progress(self, node_states: dict[NodeId, NodeState]) -> float:
        """计算任务完成百分比（0~1）。

        Args:
            node_states: NodeId 到 NodeState 的映射，记录每个节点的当前执行状态。

        Returns:
            已完成节点数占总节点数的比例，范围 0.0~1.0。空 DAG 返回 0.0。
        """
        total = len(self._nodes)
        if total == 0:
            return 0.0
        completed = sum(
            1 for nid in self._nodes
            if node_states.get(nid) is not None
            and node_states[nid].status in (NodeStatus.SUCCESS, NodeStatus.SKIPPED)
        )
        return completed / total

    def compute_max_depth(self) -> int:
        """计算 DAG 的最长路径长度（节点数）。

        拓扑排序后动态规划：从叶子到根反向计算每个节点的最长路径。
        空 DAG 返回 0，单节点返回 1。

        Returns:
            最长路径上的节点数。空 DAG 或检测到环时返回 0。
        """
        if not self._g.nodes:
            return 0
        try:
            topo = self.topological_order()
        except nx.NetworkXUnfeasible:
            # 有环时拓扑排序失败，深度无意义（环检测由 validate_structure 负责）
            return 0

        longest: dict[NodeId, int] = {}
        for nid in reversed(topo):
            max_succ = 0
            for succ in self._g.successors(nid):
                max_succ = max(max_succ, longest.get(succ, 0))
            longest[nid] = 1 + max_succ
        return max(longest.values()) if longest else 0

    # ── 图替换 ─────────────────────────────────────────────────────────────

    def replace(self, other: PlanGraph) -> None:
        """用另一个 PlanGraph 替换当前图的所有内容——全量重建 Replan 用。

        直接替换内部的 NetworkX DiGraph 和 nodes 字典，
        外部持有此 PlanGraph 引用的调用方无需替换引用。

        Args:
            other: 用于替换的 PlanGraph 实例。
        """
        self._g = other._g
        self._nodes = other._nodes

    # ── 内部访问（调试/测试用）────────────────────────────────────────────

    @property
    def _graph(self) -> nx.DiGraph:
        """直接访问 NetworkX DiGraph。仅用于调试。

        Returns:
            内部的 NetworkX DiGraph 实例。
        """
        return self._g
