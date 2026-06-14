"""PlanValidator — 校验 Planner 产出的 DAG 合法性。

图结构检查（环、依赖引用、孤立、可达性、最大深度）委托给 PlanGraph（NetworkX）。
业务检查（output_contract、节点数上限）由本模块补充。

validate() 返回 (ValidationResult, PlanGraph) 元组——
校验通过时调用方直接使用返回的 graph，无需重复构造。
"""

from __future__ import annotations

import logging

import networkx as nx

from Module.task_system.schema import (
    ConstraintConfig,
    Plan,
    TaskConstraints,
    ValidationAction,
    ValidationIssue,
    ValidationIssueType,
    ValidationResult,
)
from Module.task_system.core.graph import PlanGraph

logger = logging.getLogger(__name__)


class PlanValidator:
    """校验 Plan 的静态合法性。

    Planner 产出 Plan 后、Orchestrator 执行前调用。
    校验失败 → 返回 fix_plan（Planner 重试）或 abort（放弃）。

    当前 v1 共 8 项有效检查（2 项待 v2 补充）：
        A. 图结构（委托 PlanGraph）:
            1. 环检测
            2. 依赖引用存在
            3. 孤立节点
            4. 不可达节点
            5. 最大深度（最长路径 ≤ max_nodes，因深度上限复用节点数上限）
        B. 业务规则（本模块）:
            6. node_id 唯一性（显式复检，提供友好错误信息）
            7. output_contract 合法性（required 节点必须有 description 或 json_schema）
            8. 节点数是否超过有效 max_dag_nodes（经 TaskConstraints.resolve 合并）
        ⚠️ 待 v2 补充:
            - 工具名检查（需 DAGNode 有 tool_name 字段）
            - 高风险工具确认标记（需 RBAC 集成）
    """

    def validate(
        self,
        plan: Plan,
        global_constraints: ConstraintConfig,
        task_constraints: TaskConstraints,
    ) -> tuple[ValidationResult, PlanGraph]:
        """校验 DAG 合法性，同时返回构建好的 PlanGraph。

        调用方无需重复构造 PlanGraph——校验通过后直接使用返回的 graph，
        避免在 SubAgent 中 from_plan() 两次。

        Args:
            plan:               Planner 产出的 Plan
            global_constraints: 全局约束天花板
            task_constraints:   任务级约束（调用 resolve 后所有字段均已填充）

        Returns:
            (ValidationResult, PlanGraph) —— PlanGraph 始终返回（即使校验失败），
            失败时 graph 仍可用于调试/日志。
        """
        issues: list[ValidationIssue] = []

        # ── A. 图结构检查（PlanGraph，使用 NetworkX）─────────────────────
        graph = PlanGraph.from_plan(plan)

        # 1-4. 环检测 / 依赖引用 / 孤立 / 不可达
        structural_issues = graph.validate_structure()
        issues.extend(structural_issues)

        # 5. 最大深度 —— 复用 max_nodes 作为深度上限（深度 ≤ 节点数）。
        #    对于链状 DAG（最坏情况），深度 = 节点数。
        #    没有独立的 max_depth 字段——节点数已经约束了规模的物理上限。
        tc = task_constraints.resolve(global_constraints)
        max_nodes = tc.max_dag_nodes
        assert max_nodes is not None  # resolve() 保证所有字段已填充
        try:
            max_depth = graph.compute_max_depth()
            if max_depth > max_nodes:
                issues.append(ValidationIssue(
                    type=ValidationIssueType.MAX_DEPTH_EXCEEDED,
                    message=f"DAG 深度 {max_depth} 超过上限 {max_nodes}（深度上限 = 节点数上限）",
                ))
        except nx.NetworkXUnfeasible:
            # 有环时拓扑排序失败，环已在 structural_issues 中报告
            pass

        # ── B. 业务规则检查 ──────────────────────────────────────────────

        # 6. node_id 唯一性（PlanGraph 构建时保证，显式复检输出友好信息）
        seen: set[str] = set()
        for node in plan.dag.nodes:
            nid = str(node.node_id)
            if nid in seen:
                issues.append(ValidationIssue(
                    type=ValidationIssueType.DUPLICATE_ID,
                    node_id=node.node_id,
                    message=f"node_id 重复: {node.node_id}",
                ))
            seen.add(nid)

        # 7. output_contract 合法性
        for node in plan.dag.nodes:
            contract = node.output_contract
            # 只检查 required 节点——可选节点可以没有严格的产出契约
            if node.required and not contract.description and not contract.json_schema:
                issues.append(ValidationIssue(
                    type=ValidationIssueType.MISSING_CONTRACT,
                    node_id=node.node_id,
                    message=f"required 节点 {node.node_id} 缺少 output_contract（description 和 json_schema 均为空）",
                ))
            # JSON Schema 基本校验（如果提供了 json_schema）
            if contract.json_schema is not None:
                if not isinstance(contract.json_schema, dict):
                    issues.append(ValidationIssue(
                        type=ValidationIssueType.MISSING_CONTRACT,
                        node_id=node.node_id,
                        message=f"节点 {node.node_id} 的 output_contract.json_schema 不是有效的 dict",
                    ))
                elif "type" not in contract.json_schema:
                    issues.append(ValidationIssue(
                        type=ValidationIssueType.MISSING_CONTRACT,
                        node_id=node.node_id,
                        message=f"节点 {node.node_id} 的 output_contract.json_schema 缺少 'type' 字段",
                    ))

        # 8. 节点数检查 —— max_nodes 已通过 TaskConstraints.resolve() 合并全局与任务级约束
        if len(plan.dag.nodes) > max_nodes:
            issues.append(ValidationIssue(
                type=ValidationIssueType.MAX_NODES_EXCEEDED,
                message=f"DAG 节点数 {len(plan.dag.nodes)} 超过上限 {max_nodes}",
            ))

        # ── 汇总 ──────────────────────────────────────────────────────────
        if not issues:
            return ValidationResult(valid=True, action=ValidationAction.PROCEED), graph

        # 区分 fix_plan vs abort
        fatal_types = {
            ValidationIssueType.MAX_NODES_EXCEEDED,
            ValidationIssueType.MAX_DEPTH_EXCEEDED,
        }
        has_fatal = any(i.type in fatal_types for i in issues)
        return ValidationResult(
            valid=False,
            issues=issues,
            action=ValidationAction.ABORT if has_fatal else ValidationAction.FIX_PLAN,
        ), graph
