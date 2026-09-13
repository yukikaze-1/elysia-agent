"""PlanValidator — 校验 Planner 产出的 DAG 合法性。

图结构检查（环、依赖引用、孤立、可达性、最大深度）委托给 PlanGraph（NetworkX）。
业务检查（output_contract、节点数上限）由本模块补充。

validate() 返回 (ValidationResult, PlanGraph) 元组——
校验通过时调用方直接使用返回的 graph，无需重复构造。
"""

from __future__ import annotations

import logging

import networkx as nx

from Module.workflow_contract import (
    ConstraintConfig,
    WorkflowConstraints,
)
from Module.workflow_engine.schema import (
    ArtifactId,
    Plan,
    ValidationAction,
    ValidationIssue,
    ValidationIssueType,
    ValidationResult,
)
from Module.workflow_engine.core.graph import PlanGraph

logger = logging.getLogger(__name__)


class PlanValidator:
    """校验 Plan 的静态合法性。

    Planner 产出 Plan 后、Orchestrator 执行前调用。
    校验失败 → 返回 fix_plan（Planner 重试）或 abort（放弃）。

    当前 v1 共 9 项有效检查（2 项待 v2 补充）：
        A. 图结构（委托 PlanGraph）:
            1. 环检测
            2. 依赖引用存在
            3. 孤立节点
            4. 不可达节点
            5. 最大深度（最长路径 ≤ max_nodes，因深度上限复用节点数上限）
        B. 业务规则（本模块）:
            6. node_id 唯一性（显式复检，提供友好错误信息）
            7. output_contract 合法性（required 节点必须有 description 或 json_schema）
            8. 节点数是否超过有效 max_dag_nodes（经 WorkflowConstraints.resolve 合并）
        ⚠️ 待 v2 补充:
            - 工具名检查（需 DAGNode 有 tool_name 字段）
            - 高风险工具确认标记（需 RBAC 集成）
    """

    def validate(
        self,
        plan: Plan,
        global_constraints: ConstraintConfig,
        task_constraints: WorkflowConstraints,
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
        # ── 0. 自动修复：required 节点 output_as 为 null → 自动填充 ──
        # Planner 偶尔遗漏 output_as——与其让 PlanValidator ABORT 或进入昂贵
        # 的 FIX_PLAN 重试，不如直接补全。修复后的 plan 用于后续校验和图构建。
        for node in plan.dag.nodes:
            if node.required and not node.output_as:
                node.output_as = ArtifactId(f"output:{node.node_id}")
                logger.warning(
                    "PlanValidator: required 节点 %s 缺少 output_as，"
                    "自动填充为 %s",
                    node.node_id, node.output_as,
                )

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
        if max_nodes is None:
            raise RuntimeError("max_dag_nodes 在 resolve() 后不应为 None")
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
        seen_ids: set[str] = set()
        for node in plan.dag.nodes:
            nid = str(node.node_id)
            if nid in seen_ids:
                issues.append(ValidationIssue(
                    type=ValidationIssueType.DUPLICATE_ID,
                    node_id=node.node_id,
                    message=f"node_id 重复: {node.node_id}",
                ))
            seen_ids.add(nid)

        # 6b. output_as 唯一性——重复的 output_as 会导致 ArtifactStore 静默覆盖
        seen_outputs: set[str] = set()
        for node in plan.dag.nodes:
            oa = node.output_as
            if oa and not isinstance(oa, str):
                continue  # ArtifactId 实例，str() 后检查
            oa_str = str(oa) if oa else None
            if oa_str is None:
                continue
            if oa_str in seen_outputs:
                issues.append(ValidationIssue(
                    type=ValidationIssueType.DUPLICATE_ID,
                    node_id=node.node_id,
                    message=f"output_as 重复: {oa_str}（多个节点输出到同一 key，"
                            f"ArtifactStore 会静默覆盖）",
                ))
            seen_outputs.add(oa_str)

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

        # 8. 节点数检查 —— max_nodes 已通过 WorkflowConstraints.resolve() 合并全局与任务级约束
        if len(plan.dag.nodes) > max_nodes:
            issues.append(ValidationIssue(
                type=ValidationIssueType.MAX_NODES_EXCEEDED,
                message=f"DAG 节点数 {len(plan.dag.nodes)} 超过上限 {max_nodes}",
            ))

        # 9. 至少一个 required 节点（S2-D5 的业务侧）
        #    空图由 `validate_structure` 报（第 0 项）；这里管"有节点但全 optional"，
        #    它对"全部 required 成功"同样空真——任务会以 0/0 假成功收尾。
        if plan.dag.nodes and not any(n.required for n in plan.dag.nodes):
            issues.append(ValidationIssue(
                type=ValidationIssueType.EMPTY_DAG,
                message=f"DAG 有 {len(plan.dag.nodes)} 个节点但**没有一个 required**——"
                        f"「全部 required 成功」空真，任务会假成功收尾",
            ))

        # 10. input_bindings ↔ output_as ↔ 拓扑前驱 的交叉一致性（S2-D7）
        #     `input_bindings`（数据依赖）与 `edges`（执行依赖）是**两套独立声明**：
        #     就绪判定只看 predecessors。LLM 产出两者不一致时（漏边、拼错 output:、
        #     引用了已被全量重建丢弃的节点），节点会**早于/脱离**上游执行，由 LLM
        #     自行编造内容，Evaluator 仍可能判成功 → 静默错误结果并计入"成功节点"。
        producer = {
            str(n.output_as): n.node_id
            for n in plan.dag.nodes if n.output_as
        }
        for node in plan.dag.nodes:
            for aid in node.input_bindings:
                key = str(aid)
                owner = producer.get(key)
                if owner is None:
                    issues.append(ValidationIssue(
                        type=ValidationIssueType.MISSING_DEPENDENCY,
                        node_id=node.node_id,
                        message=f"input_bindings 引用了没有任何节点产出的 {key}"
                                f"（没有节点的 output_as 等于它）",
                    ))
                    continue
                if owner == node.node_id:
                    continue
                try:
                    is_pred = graph.is_predecessor(owner, node.node_id)
                except Exception:                     # noqa: BLE001 未知节点等
                    is_pred = False
                if not is_pred:
                    issues.append(ValidationIssue(
                        type=ValidationIssueType.MISSING_DEPENDENCY,
                        node_id=node.node_id,
                        message=f"input_bindings 引用 {key}（由 {owner} 产出），"
                                f"但 {owner} 不是它的拓扑前驱——缺 edges 声明，"
                                f"节点可能先于上游执行并读到缺失数据",
                    ))

        # ── 汇总 ──────────────────────────────────────────────────────────
        if not issues:
            return ValidationResult(valid=True, action=ValidationAction.PROCEED), graph

        # 区分 fix_plan vs abort
        fatal_types = {
            ValidationIssueType.MAX_NODES_EXCEEDED,
            ValidationIssueType.MAX_DEPTH_EXCEEDED,
            # S2-D5：空图/无 required 不可"修复计划"——重规划也是空图，直接 ABORT，
            # 避免"再试一次仍然假成功"。
            ValidationIssueType.EMPTY_DAG,
        }
        has_fatal = any(i.type in fatal_types for i in issues)
        return ValidationResult(
            valid=False,
            issues=issues,
            action=ValidationAction.ABORT if has_fatal else ValidationAction.FIX_PLAN,
        ), graph
