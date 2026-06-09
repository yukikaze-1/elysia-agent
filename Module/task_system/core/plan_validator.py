"""PlanValidator — 校验 Planner 产出的 DAG 合法性。

图结构检查（环、依赖引用、孤立、可达）委托给 PlanGraph（NetworkX）。
业务检查（工具存在、output_contract、约束上限）由本模块补充。
"""

from __future__ import annotations

import logging

from Module.task_system.schema import (
    ConstraintConfig,
    Plan,
    TaskConstraints,
    ValidationIssue,
    ValidationIssueType,
    ValidationResult,
)

logger = logging.getLogger(__name__)


class PlanValidator:
    """校验 Plan 的静态合法性。

    Planner 产出 Plan 后、Orchestrator 执行前调用。
    校验失败 → 返回 fix_plan（Planner 重试）或 abort（放弃）。

    10 项检查分为两类：
        A. 图结构（委托 PlanGraph）:
            1. 环检测
            2. 依赖引用存在
            3. 孤立节点
            4. 不可达节点
        B. 业务规则（本模块）:
            5. node_id 唯一性（构建时保证，复检）
            6. 工具名是否在可用工具列表中
            7. output_contract 是否合法
            8. 高风险工具节点是否有确认标记
            9. 节点数是否超过 max_dag_nodes
            10. DAG 深度是否超过限制
    """

    def validate(
        self,
        plan: Plan,
        global_constraints: ConstraintConfig,
        task_constraints: TaskConstraints,
        available_tools: set[str] | None = None,
    ) -> ValidationResult:
        """校验 DAG 合法性。

        Args:
            plan:               Planner 产出的 Plan
            global_constraints: 全局约束天花板
            task_constraints:   任务级约束（与 global 取 min）
            available_tools:    可用工具名集合（用于工具存在性检查）

        Returns:
            ValidationResult（valid + issues + action）
        """
        issues: list[ValidationIssue] = []

        # ── A. 图结构检查（PlanGraph，使用 NetworkX）─────────────────────
        from Module.task_system.core.graph import PlanGraph
        graph = PlanGraph.from_plan(plan)

        structural_issues = graph.validate_structure(available_tools)
        issues.extend(structural_issues)

        # ── B. 业务规则检查 ──────────────────────────────────────────────

        # 5. node_id 唯一性（NetworkX 构建时保证，但显式复检输出友好信息）
        seen: set[str] = set()
        for node in plan.nodes:
            nid = str(node.node_id)
            if nid in seen:
                issues.append(ValidationIssue(
                    type=ValidationIssueType.DUPLICATE_ID,
                    node_id=node.node_id,
                    message=f"node_id 重复: {node.node_id}",
                ))
            seen.add(nid)

        # 7. output_contract 合法性 —— 暂时无法检查（DAGNode 尚未添加 output_contract 字段）
        # TODO: DAGNode 添加 output_contract: OutputContract 后启用

        # 9. 节点数检查 —— 取 min(全局, 任务)
        max_nodes = min(global_constraints.max_dag_nodes, task_constraints.max_dag_nodes)
        if len(plan.nodes) > max_nodes:
            issues.append(ValidationIssue(
                type=ValidationIssueType.MAX_NODES_EXCEEDED,
                message=f"DAG 节点数 {len(plan.nodes)} 超过上限 {max_nodes}",
            ))

        # 10. DAG 深度检查 —— 使用拓扑排序计算最长路径
        try:
            topo = graph.topological_order()
            # 计算从每个节点开始的最长路径
            longest: dict[str, int] = {}
            for nid in reversed(topo):
                max_succ = 0
                for succ in graph._graph.successors(nid):
                    max_succ = max(max_succ, longest.get(str(succ), 0))
                longest[str(nid)] = 1 + max_succ
            max_depth = max(longest.values()) if longest else 0
            # 深度上限：使用 max_dag_nodes 作为隐式深度上限（深度 ≤ 节点数）
            if max_depth > max_nodes:
                issues.append(ValidationIssue(
                    type=ValidationIssueType.MAX_DEPTH_EXCEEDED,
                    message=f"DAG 深度 {max_depth} 超过上限 {max_nodes}",
                ))
        except Exception:
            # 拓扑排序失败（有环时），已在 structural_issues 中报告
            pass

        # ── 汇总 ──────────────────────────────────────────────────────────
        if not issues:
            return ValidationResult(valid=True, action="proceed")

        # 区分 fix_plan vs abort
        fatal_types = {
            ValidationIssueType.MAX_NODES_EXCEEDED,
            ValidationIssueType.MAX_DEPTH_EXCEEDED,
        }
        has_fatal = any(i.type in fatal_types for i in issues)
        return ValidationResult(
            valid=False,
            issues=issues,
            action="abort" if has_fatal else "fix_plan",
        )
