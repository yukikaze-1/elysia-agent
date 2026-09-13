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

from Module.workflow_contract import NodeId
from Module.workflow_engine.schema import (
    DAGNode,
    DAGNodeInfo,
    DAGPatch,
    NodeState,
    NodeStatus,
    WorkflowDAG,
    ValidationIssue,
    ValidationIssueType,
)

if TYPE_CHECKING:
    from Module.workflow_engine.schema import Plan

logger = logging.getLogger(__name__)


class PlanGraph:
    """NetworkX DiGraph 封装——Plan 的图算法层。

    edges 的方向: dep → node（dep 必须先完成）。
    例: A → B 表示 B 依赖 A，A 完成后 B 才能执行。
    """

    def __init__(self, g: nx.DiGraph, nodes: dict[NodeId, DAGNode],
                 dropped_edges: list[tuple[str, str]] | None = None) -> None:
        """初始化 PlanGraph。

        Args:
            g: NetworkX 有向图，节点为 NodeId。
            nodes: NodeId 到 DAGNode 的映射，保存节点的完整元数据。
            dropped_edges: `from_plan` 因端点不存在而丢弃的边（S2-D8）——
                留痕供 `validate_structure` 报成 `MISSING_DEPENDENCY`，
                而不是无声地把依赖删掉。
        """
        self._g = g
        self._nodes = nodes  # NodeId → DAGNode（含 description, output_contract, required 等）
        self._dropped_edges: list[tuple[str, str]] = list(dropped_edges or [])

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

        # S2-D8：端点不存在的边**不能静默丢弃**。原先只 warning 后 continue，
        # 而 `validate_structure` 里那条"依赖的节点不存在"检查遍历的是
        # `networkx.predecessors()`——networkx 只可能返回图中**已存在**的节点，
        # 该分支永远不可能命中（`MISSING_DEPENDENCY` 成了"有兜底、无来源"的假保护）。
        # 于是 LLM 写错的边既不报问题也不失败，只是**依赖被静默删除**：下游节点
        # 不再等上游，可能先执行并读到缺失产物。
        dropped_edges: list[tuple[str, str]] = []
        for from_id, to_id in plan.dag.edges:
            if from_id not in g.nodes or to_id not in g.nodes:
                logger.warning("PlanGraph: edge 端点不存在 (%s → %s)", from_id, to_id)
                dropped_edges.append((str(from_id), str(to_id)))
                continue
            g.add_edge(from_id, to_id)

        return cls(g, nodes, dropped_edges)

    def to_plan(self, plan: Plan | None = None) -> Plan:
        """将当前图状态写回 Plan（更新 dag.nodes + dag.edges）。

        若 plan 为 None，创建新 Plan。调用方通常传入已有 Plan 实例。

        Args:
            plan: 要更新的 Plan 实例。为 None 时创建新 Plan。

        Returns:
            更新后的 Plan 实例。
        """
        from Module.workflow_engine.schema import (
            DAG,
            Plan,
        )
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

    @property
    def dropped_edges(self) -> list[tuple[str, str]]:
        """`from_plan` 因端点不存在而丢弃的边（S2-D8，只读）。

        对外暴露是为了让校验/诊断/测试能看见"依赖被删掉了"这一事实——原先它只活在
        一条 warning 日志里。
        """
        return list(self._dropped_edges)

    def is_predecessor(self, maybe_pred: NodeId, node_id: NodeId) -> bool:
        """`maybe_pred` 是否是 `node_id` 的（直接或间接）前驱。

        S2-D7 用：`input_bindings` 声明的产物提供者必须是拓扑前驱——否则下游会
        早于上游执行。用 `nx.ancestors` 而非直接前驱：允许中间节点被省略。

        Args:
            maybe_pred: 候选前驱节点 ID。
            node_id: 目标节点 ID。

        Returns:
            True 表示存在从 maybe_pred 到 node_id 的路径。
        """
        if maybe_pred not in self._g.nodes or node_id not in self._g.nodes:
            return False
        return maybe_pred in nx.ancestors(self._g, node_id)

    def is_terminal(self, node_states: dict[NodeId, NodeState]) -> bool:
        """所有 required=True 的节点都是 SUCCESS 状态——**空图不算完成**。

        S2-D5：原先空图会**空真**返回 True（"所有 required 都成功"对一个没有节点的
        图成立），于是 `Orchestrator.run()` 的 `while not task_state.is_terminal()`
        立即退出、直接 finalize 成 COMPLETED（0/0 节点）。用户收到"任务完成"而实际
        什么都没做，且 `WorkflowState.error` 恒空——事后从查询面看不出异常。
        `compute_progress()` 空图返回 0.0 而本方法返回 True，两者语义冲突也没人对齐。

        Args:
            node_states: NodeId 到 NodeState 的映射，记录每个节点的当前执行状态。

        Returns:
            如果存在节点且所有必需节点都已成功完成则返回 True，否则返回 False。
        """
        if not self._nodes:
            return False
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

        # 0. 空图不变量（S2-D5）
        #    `is_terminal()` 的语义是"所有 required 节点都 SUCCESS"——对空图**空真**。
        #    可达路径：单节点 required 失败 → REPLAN → Replanner 用 remove_nodes
        #    清空图（prompt 允许）→ patch 后校验通过 → 下次 run() 立即 break →
        #    finalize() → **COMPLETED（0/0 节点）**。用户收到"任务完成"而实际什么
        #    都没做。这里把它变成显式校验问题。
        if len(self._g.nodes) == 0:
            issues.append(ValidationIssue(
                type=ValidationIssueType.EMPTY_DAG,
                message="DAG 为空——没有任何节点；空图对「全部 required 成功」空真，"
                        "会被判成完成。",
            ))
            return issues

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
        #    注意：`predecessors()` 只可能返回图中已存在的节点，故下面的循环**恒不命中**
        #    ——真正需要报的是 `from_plan` 丢弃的那些边（S2-D8），它们本来会让这个
        #    分支变成"有兜底、无来源"的假保护。两者都保留：前者防未来实现变化，
        #    后者是有来源的那一条。
        for node_id in self._g.nodes:
            for pred in self._g.predecessors(node_id):
                if pred not in self._g.nodes:
                    issues.append(ValidationIssue(
                        type=ValidationIssueType.MISSING_DEPENDENCY,
                        node_id=node_id,
                        message=f"依赖的节点 {pred} 不存在",
                    ))
        for from_id, to_id in self._dropped_edges:
            issues.append(ValidationIssue(
                type=ValidationIssueType.MISSING_DEPENDENCY,
                node_id=NodeId(to_id) if to_id in self._g.nodes else None,
                message=f"边 {from_id} → {to_id} 的端点不存在，依赖被删除"
                        f"（下游不再等待上游，可能读到缺失产物）",
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
    ) -> WorkflowDAG:
        """构建 Debug WebUI 用的 DAG 视图。

        Args:
            node_states: NodeId 到 NodeState 的映射，记录每个节点的当前执行状态。

        Returns:
            包含节点信息（DAGNodeInfo）和边列表的 WorkflowDAG 视图。
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

        return WorkflowDAG(nodes=dag_nodes, edges=edges)

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
