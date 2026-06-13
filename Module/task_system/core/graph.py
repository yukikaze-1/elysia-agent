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
        self._g = g
        self._nodes = nodes  # NodeId → DAGNode（含 description, output_contract, required 等）

    # ── 构造 / 导出 ──────────────────────────────────────────────────────────

    @classmethod
    def from_plan(cls, plan: Plan) -> PlanGraph:
        """从 Pydantic Plan 构建 PlanGraph。

        plan.dag 持有 nodes + edges，直接建图，无需遍历 node.depends_on。
        """
        g = nx.DiGraph()
        nodes: dict[NodeId, DAGNode] = {}

        for node in plan.dag.nodes:
            if node.node_id in nodes:
                logger.warning(
                    "PlanGraph.from_plan: 重复 node_id=%s，后面的覆盖前面的",
                    node.node_id,
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
        return len(self._g.nodes)

    @property
    def nodes(self) -> dict[NodeId, DAGNode]:
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
        """拓扑排序——Executor 的调度顺序。"""
        return list(nx.topological_sort(self._g))

    def is_terminal(self, node_states: dict[NodeId, NodeState]) -> bool:
        """所有 required=True 的节点都是 SUCCESS 状态。"""
        for node_id, node in self._nodes.items():
            if not node.required:
                continue
            ns = node_states.get(node_id)
            if ns is None or ns.status != NodeStatus.SUCCESS:
                return False
        return True

    def has_failed_required(self, node_states: dict[NodeId, NodeState]) -> bool:
        """是否有 required=True 的节点处于 FAILED 或 UNRECOVERABLE。"""
        for node_id, node in self._nodes.items():
            if not node.required:
                continue
            ns = node_states.get(node_id)
            if ns is not None and ns.status in (NodeStatus.FAILED, NodeStatus.UNRECOVERABLE):
                return True
        return False

    def collect_failure_info(self, node_states: dict[NodeId, NodeState]) -> dict:
        """收集失败节点的诊断信息，供 Sub-Agent 传给 Planner.replan()。"""
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

        顺序保证了依赖一致性：
            1. replace_nodes — 先删旧节点，再加新节点
            2. remove_nodes  — 移除不需要的节点
            3. add_nodes     — 新增节点
            4. remove_edges  — 移除不需要的边
            5. update_edges  — 新增边

        full_replan 不在此处理——由 Sub-Agent 层创建新 Plan + 重建 PlanGraph。
        """
        # 1. 替换节点（先删后加，新节点可能不同 node_id）
        for old_id, new_node in patch.replace_nodes:
            if old_id in self._g.nodes:
                self._g.remove_node(old_id)
                self._nodes.pop(old_id, None)
            self._g.add_node(new_node.node_id)
            self._nodes[new_node.node_id] = new_node

        # 2. 移除节点
        for node_id in patch.remove_nodes:
            if node_id in self._g.nodes:
                self._g.remove_node(node_id)
                self._nodes.pop(node_id, None)

        # 3. 新增节点
        for node in patch.add_nodes:
            self._g.add_node(node.node_id)
            self._nodes[node.node_id] = node

        # 4. 移除边
        for from_id, to_id in patch.remove_edges:
            if self._g.has_edge(from_id, to_id):
                self._g.remove_edge(from_id, to_id)

        # 5. 新增边
        for from_id, to_id in patch.update_edges:
            if from_id in self._g.nodes and to_id in self._g.nodes:
                self._g.add_edge(from_id, to_id)

    # ── 查询视图 ─────────────────────────────────────────────────────────────

    def to_dag_view(
        self, node_states: dict[NodeId, NodeState],
    ) -> TaskDAG:
        """构建 Debug WebUI 用的 DAG 视图。"""
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
        """计算任务完成百分比（0~1）。"""
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

    # ── 内部访问（调试/测试用）────────────────────────────────────────────

    @property
    def _graph(self) -> nx.DiGraph:
        """直接访问 NetworkX DiGraph。仅用于调试。"""
        return self._g
