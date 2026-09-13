# 测试文件：Module/workflow_engine/tests/unit/test_plan_invariants.py
"""计划校验的三个不变量（S2-D5 / D7 / D8）——共同根因是"假保护"。

| 项 | 原状 | 后果 |
|---|---|---|
| **D5** | 空图对"所有 required 节点都 SUCCESS"**空真** | 任务以 COMPLETED(0/0) **假成功**收尾；`WorkflowState.error` 恒空，事后查不出来 |
| **D7** | `input_bindings`（数据依赖）与 `edges`（执行依赖）**两套独立声明**、互不校验 | 节点早于/脱离上游执行，LLM 自行编造内容，Evaluator 仍可能判成功 → **静默错误结果** |
| **D8** | 端点不存在的边被 `from_plan` **静默丢弃**；`MISSING_DEPENDENCY` 分支恒不可达 | LLM 写错的边既不报问题也不失败，**依赖被静默删除** |

D8 那条尤其值得记：`validate_structure` 的第 2 项遍历 `nx.predecessors()` 找"不存在的
依赖"——而 networkx **只返回图中已存在的节点**，该分支永远不可能命中。
`ValidationIssueType.MISSING_DEPENDENCY` 在 `_abort_error_code` 里被列为 plan_quality
（设计上认为它会导致 ABORT），**却没有生产者**：有兜底、无来源。
"""

from unittest.mock import MagicMock

import pytest

from Module.workflow_contract import ConstraintConfig, NodeId, WorkflowStatus
from Module.workflow_engine.core.graph import PlanGraph
from Module.workflow_engine.core.plan_validator import PlanValidator
from Module.workflow_engine.schema import (
    ArtifactId,
    DAG,
    DAGNode,
    NodeState,
    NodeStatus,
    OutputContract,
    Plan,
    ValidationIssueType,
)

pytestmark = pytest.mark.unit


def _node(nid: str, *, required: bool = True, output_as: str | None = None,
          bindings: list[str] | None = None, description: str = "做事") -> DAGNode:
    return DAGNode(
        node_id=NodeId(nid),
        description=description,
        output_contract=OutputContract(description="产出"),
        required=required,
        output_as=ArtifactId(output_as) if output_as else None,
        input_bindings=[ArtifactId(b) for b in (bindings or [])],
    )


def _plan(nodes: list[DAGNode], edges: list[tuple[str, str]] | None = None) -> Plan:
    return Plan(
        dag=DAG(nodes=nodes, edges=[(NodeId(a), NodeId(b)) for a, b in (edges or [])]),
        plan_version=1,
    )


def _result(plan: Plan):
    """跑一遍校验器，返回 (ValidationResult, PlanGraph)。

    `task_constraints` 用空 `WorkflowConstraints()`——`resolve()` 会把未填字段
    回退到全局天花板，故 `max_dag_nodes` 不会是 None。
    """
    from Module.workflow_contract import WorkflowConstraints

    return PlanValidator().validate(plan, ConstraintConfig(), WorkflowConstraints())


# ═══════════════════════════════════════════════════════════════════
# S2-D5：空图不得被判"完成"
# ═══════════════════════════════════════════════════════════════════

class TestEmptyDagIsNotTerminal:
    def test_empty_graph_is_not_terminal(self):
        """**核心**：空图的 `is_terminal()` 必须为 False。

        原语义「所有 required 节点都 SUCCESS」对空图**空真** → `Orchestrator.run()`
        的 `while not task_state.is_terminal()` 立即退出 → finalize 成 COMPLETED(0/0)。
        """
        graph = PlanGraph.from_plan(_plan([]))
        assert graph.is_terminal({}) is False

    def test_non_empty_all_success_is_terminal(self):
        """回归网：正常图的行为不变。"""
        graph = PlanGraph.from_plan(_plan([_node("a", output_as="output:a")]))
        states = {NodeId("a"): NodeState(node_id=NodeId("a"), status=NodeStatus.SUCCESS)}
        assert graph.is_terminal(states) is True

    def test_validate_structure_reports_empty_dag(self):
        issues = PlanGraph.from_plan(_plan([])).validate_structure()
        assert [i.type for i in issues] == [ValidationIssueType.EMPTY_DAG]

    def test_validator_aborts_on_empty_dag(self):
        """空图**不可修**——重规划仍是空图，必须 ABORT 而不是 FIX_PLAN 再来一次。"""
        result, _ = _result(_plan([]))
        assert result.valid is False
        assert ValidationIssueType.EMPTY_DAG in {i.type for i in result.issues}
        assert result.action.value == "abort"

    def test_validator_rejects_all_optional_nodes(self):
        """有节点但**一个 required 都没有**——对同一空真语义，同样要拦。"""
        result, _ = _result(_plan([_node("a", required=False, output_as="output:a")]))
        assert result.valid is False
        assert ValidationIssueType.EMPTY_DAG in {i.type for i in result.issues}


# ═══════════════════════════════════════════════════════════════════
# S2-D7：input_bindings ↔ output_as ↔ 拓扑前驱
# ═══════════════════════════════════════════════════════════════════

class TestInputBindingConsistency:
    def test_binding_without_producer_is_reported(self):
        """引用一个没有任何节点产出的 key → 报 MISSING_DEPENDENCY。"""
        result, _ = _result(_plan(
            [_node("a", output_as="output:a", bindings=["output:ghost"])]))
        msgs = [i.message for i in result.issues
                if i.type == ValidationIssueType.MISSING_DEPENDENCY]
        assert any("output:ghost" in m for m in msgs), result.issues

    def test_binding_producer_must_be_predecessor(self):
        """产物有生产者、但**没有 edges 声明依赖** → 同样要报。

        这是最典型的一种：LLM 写了 `input_bindings` 却漏了 `edges`。就绪判定只看
        predecessors，于是下游会在上游之前执行、读到缺失产物。
        """
        result, _ = _result(_plan([
            _node("a", output_as="output:a"),
            _node("b", output_as="output:b", bindings=["output:a"]),
        ]))          # 注意：没有 (a, b) 这条边
        msgs = [i.message for i in result.issues
                if i.type == ValidationIssueType.MISSING_DEPENDENCY]
        assert any("拓扑前驱" in m for m in msgs), result.issues

    def test_consistent_plan_passes(self):
        """回归网：有边、有产物、有 binding 的一致计划必须通过。"""
        result, _ = _result(_plan([
            _node("a", output_as="output:a"),
            _node("b", output_as="output:b", bindings=["output:a"]),
        ], edges=[("a", "b")]))
        assert result.valid is True, result.issues

    def test_transitive_predecessor_is_accepted(self):
        """中间节点被省略（a→c 间接）也算满足——用 ancestors 而非直接前驱。"""
        result, _ = _result(_plan([
            _node("a", output_as="output:a"),
            _node("b", output_as="output:b"),
            _node("c", output_as="output:c", bindings=["output:a"]),
        ], edges=[("a", "b"), ("b", "c")]))
        assert result.valid is True, result.issues


# ═══════════════════════════════════════════════════════════════════
# S2-D8：丢边必须留痕
# ═══════════════════════════════════════════════════════════════════

class TestDroppedEdgesAreReported:
    def test_dropped_edge_recorded_and_reported(self):
        graph = PlanGraph.from_plan(_plan(
            [_node("a", output_as="output:a")], edges=[("a", "ghost")]))
        assert graph.dropped_edges == [("a", "ghost")]
        issues = graph.validate_structure()
        missing = [i for i in issues if i.type == ValidationIssueType.MISSING_DEPENDENCY]
        assert len(missing) == 1
        assert "ghost" in missing[0].message

    def test_validator_surfaces_dropped_edge_as_fix_plan(self):
        """经过校验器也要报出来（走 FIX_PLAN：重规划能补回这条边）。"""
        result, _ = _result(_plan(
            [_node("a", output_as="output:a"), _node("b", output_as="output:b")],
            edges=[("a", "b"), ("a", "ghost")]))
        assert result.valid is False
        assert ValidationIssueType.MISSING_DEPENDENCY in {i.type for i in result.issues}

    def test_no_dropped_edges_on_healthy_plan(self):
        graph = PlanGraph.from_plan(_plan(
            [_node("a", output_as="output:a"), _node("b", output_as="output:b")],
            edges=[("a", "b")]))
        assert graph.dropped_edges == []


# ═══════════════════════════════════════════════════════════════════
# 执行期兜底：空图不得进执行循环
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_orchestrator_refuses_empty_graph():
    """执行期的最后一道：绕过校验的空图也必须 FAILED，而不是 COMPLETED(0/0)。"""
    from Module.workflow_engine.core.orchestrator import Orchestrator
    from Module.workflow_engine.schema import WorkflowState

    orchestrator = Orchestrator(
        infra=MagicMock(),
        hitl_notifier=MagicMock(),
        executor=MagicMock(),
        evaluator=MagicMock(),
        checkpoint_manager=MagicMock(),
        artifact_store=MagicMock(),
    )
    ts = WorkflowState(
        task_id="t1", status=WorkflowStatus.RUNNING, tools=[], session_id="s1",
    )
    result = await orchestrator.run(ts, PlanGraph.from_plan(_plan([])), MagicMock())

    assert result.status.value == "failed", result
    assert "空" in result.error_context.get("reason", "")
