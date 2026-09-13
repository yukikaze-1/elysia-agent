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

from unittest.mock import AsyncMock, MagicMock

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


# ═══════════════════════════════════════════════════════════════════
# S2-D6：任务级失败原因必须落到 WorkflowState（而不是只活在邮箱事件里）
# ═══════════════════════════════════════════════════════════════════

class TestTaskLevelErrorIsRecorded:
    """`WorkflowState.error` / `error_code` 是 `WorkflowSummary`、debug 快照、前端
    TaskCard 的字段，而**没有任何生产代码写过它们**——所有任务级失败原因只活在邮箱
    事件的 `data["error"]` 里。于是用户/调试面无法区分"规划失败/工具失败/内部 bug"，
    `WorkflowErrorCode` 的 10 项里实际只有 3 项有生产者。
    """

    def test_fail_records_reason_and_code(self):
        from Module.workflow_engine.schema import WorkflowErrorCode, WorkflowState

        ts = WorkflowState(task_id="t1", tools=[], session_id="s1")
        ts.fail("规划失败：LLM 返回非 JSON", WorkflowErrorCode.PLANNING_FAILED)

        assert ts.status.value == "failed"
        assert ts.error == "规划失败：LLM 返回非 JSON"
        assert ts.error_code == WorkflowErrorCode.PLANNING_FAILED

    def test_cancel_records_reason_and_code(self):
        from Module.workflow_engine.schema import WorkflowErrorCode, WorkflowState

        ts = WorkflowState(task_id="t1", tools=[], session_id="s1")
        ts.cancel("用户取消")
        assert ts.status.value == "cancelled"
        assert ts.error == "用户取消"
        assert ts.error_code == WorkflowErrorCode.CANCELLED_BY_USER

    def test_fail_without_code_keeps_existing(self):
        """不传 code 时保留既有值——不把已定位的原因覆盖成 None。"""
        from Module.workflow_engine.schema import WorkflowErrorCode, WorkflowState

        ts = WorkflowState(task_id="t1", tools=[], session_id="s1")
        ts.fail("第一次", WorkflowErrorCode.TIMEOUT)
        ts.fail("第二次")
        assert ts.error == "第二次"
        assert ts.error_code == WorkflowErrorCode.TIMEOUT

    @pytest.mark.asyncio
    async def test_handle_failed_writes_task_state(self):
        """三个失败处理器之一：`handle_failed` 必须把 error/code 落到任务状态。"""
        from Module.workflow_engine.core.sub_agent.result_handlers import handle_failed
        from Module.workflow_engine.schema import (
            OrchestratorResult,
            OrchestratorRunStatus,
            WorkflowErrorCode,
            WorkflowState,
        )

        ts = WorkflowState(task_id="t1", tools=[], session_id="s1")
        result = OrchestratorResult(
            status=OrchestratorRunStatus.FAILED,
            error_context={"reason": "工具连续失败", "error_type": "timeout"},
        )
        pushed: list[tuple] = []

        async def _push(mailbox, etype, data):
            pushed.append((etype, data))

        await handle_failed(result, ts, MagicMock(), _push)

        assert ts.error == "工具连续失败"
        assert ts.error_code == WorkflowErrorCode.TIMEOUT, "编排层给的码要如实落库"
        assert pushed and pushed[0][1]["error_type"] == "timeout"

    @pytest.mark.asyncio
    async def test_handle_failed_unknown_code_falls_back(self):
        """编排层给了未登记的码 → 兜底 INTERNAL_ERROR，不抛。"""
        from Module.workflow_engine.core.sub_agent.result_handlers import handle_failed
        from Module.workflow_engine.schema import (
            OrchestratorResult,
            OrchestratorRunStatus,
            WorkflowErrorCode,
            WorkflowState,
        )

        ts = WorkflowState(task_id="t1", tools=[], session_id="s1")
        result = OrchestratorResult(
            status=OrchestratorRunStatus.FAILED,
            error_context={"reason": "x", "error_type": "not_a_real_code"},
        )

        async def _push(mailbox, etype, data):
            return None

        await handle_failed(result, ts, MagicMock(), _push)
        assert ts.error_code == WorkflowErrorCode.INTERNAL_ERROR


class TestPlanningFailureIsNotInternalError:
    """规划阶段的 `PlanParseError` 原先**没有 try/except**：冒泡到 `_run_and_cleanup`
    的兜底分支被统一报成"任务执行异常" + `internal_error`。而 `DAGNode(**...)` 非法
    字段抛的是 `pydantic.ValidationError`，会**穿透** `PlanParseError` 的捕获。
    不对称容错：Evaluator 有重试、Planner 没有 → 一次 LLM 抖动判死整个任务。
    """

    @pytest.mark.asyncio
    async def test_plan_parse_error_retries_then_reports_planning_failed(self):
        from Module.workflow_engine.core.sub_agent.planning import _plan_with_retry
        from Module.workflow_engine.core.planner.parser import PlanParseError
        from Module.workflow_engine.schema import WorkflowErrorCode, WorkflowState

        planner = MagicMock()
        planner.plan = AsyncMock(side_effect=PlanParseError("LLM 返回非 JSON"))
        ts = WorkflowState(task_id="t1", tools=[], session_id="s1")

        result = await _plan_with_retry(planner, MagicMock(), MagicMock(), ts, "t1")

        assert result is None
        assert planner.plan.await_count == 2, "规划必须有重试（与 Evaluator 对称）"
        assert ts.error_code == WorkflowErrorCode.PLANNING_FAILED, (
            f"必须是 PLANNING_FAILED 而不是 INTERNAL_ERROR：{ts.error_code}")
        assert "规划失败" in (ts.error or "")

    @pytest.mark.asyncio
    async def test_validation_error_is_also_caught(self):
        """`pydantic.ValidationError`（DAGNode 非法字段）不能被漏掉。"""
        from pydantic import ValidationError

        from Module.workflow_engine.core.sub_agent.planning import _plan_with_retry
        from Module.workflow_engine.schema import WorkflowErrorCode, WorkflowState

        planner = MagicMock()
        planner.plan = AsyncMock(side_effect=ValidationError.from_exception_data(
            "DAGNode", [{"type": "missing", "loc": ("node_id",), "input": {}}]))
        ts = WorkflowState(task_id="t1", tools=[], session_id="s1")

        assert await _plan_with_retry(planner, MagicMock(), MagicMock(), ts, "t1") is None
        assert ts.error_code == WorkflowErrorCode.PLANNING_FAILED

    @pytest.mark.asyncio
    async def test_success_after_retry_returns_plan(self):
        """回归网：第一次抖动、第二次成功 → 返回 Plan，不写失败。"""
        from Module.workflow_engine.core.sub_agent.planning import _plan_with_retry
        from Module.workflow_engine.core.planner.parser import PlanParseError
        from Module.workflow_engine.schema import WorkflowState

        good = MagicMock()
        planner = MagicMock()
        planner.plan = AsyncMock(side_effect=[PlanParseError("抖动"), good])
        ts = WorkflowState(task_id="t1", tools=[], session_id="s1")

        assert await _plan_with_retry(planner, MagicMock(), MagicMock(), ts, "t1") is good
        assert ts.error_code is None


# ═══════════════════════════════════════════════════════════════════
# S2-D3：重试不得是"盲重跑"
# ═══════════════════════════════════════════════════════════════════

class TestRetryCarriesFailureReason:
    """失败重试原先是**盲重跑**：`NodeState.error` 会被记下，但
    `Executor.execute()` 的签名里没有它，重试 prompt 与首次**逐字节相同**。
    LLM 拿不到任何新信息，只能原样再选一次同样的工具、同样的参数——对"副作用已产生
    但返回失败"的工具（典型：执行成功但响应超时）就是重复产生副作用。
    """

    def test_hint_is_absent_on_first_attempt(self):
        from Module.workflow_engine.core.executor import _retry_hint

        assert _retry_hint(None) is None
        assert _retry_hint("") is None

    def test_hint_mentions_reason_and_forbids_blind_repeat(self):
        from Module.workflow_engine.core.executor import _retry_hint

        hint = _retry_hint("工具超时：write_file 30s 未返回")
        assert hint is not None
        assert "write_file" in hint, "原因要原样带给 LLM"
        assert "不要原样重复" in hint, "必须明确禁止盲重跑"
        assert "副作用" in hint, "要提醒「可能已执行」的情形"

    @pytest.mark.asyncio
    async def test_execute_passes_previous_error_into_prompt(self):
        """接线：`execute(previous_error=...)` 必须出现在发给 LLM 的 user message 里。"""
        from Module.workflow_engine.core.executor import Executor
        from Module.workflow_engine.schema import ArtifactId, DAGNode, OutputContract
        from Module.workflow_contract import NodeId

        captured: dict = {}

        class _Completions:
            async def create(self, **kwargs):
                captured.update(kwargs)
                msg = MagicMock()
                msg.tool_calls = None
                msg.content = "完成"
                choice = MagicMock()
                choice.message = msg
                resp = MagicMock()
                resp.choices = [choice]
                resp.usage = None
                return resp

        llm = MagicMock()
        llm.chat.completions.create = _Completions().create
        infra = MagicMock()
        infra.llm = llm
        infra.llm_model = "test"
        infra.tool_registry.definitions.return_value = []

        executor = Executor(infra=infra, artifact_store=MagicMock(),
                            hitl_notifier=MagicMock(), token_ledger=None)
        node = DAGNode(
            node_id=NodeId("n1"), description="写文件",
            output_contract=OutputContract(description="结果"),
            output_as=ArtifactId("output:n1"),
        )
        ts = MagicMock()

        await executor.execute(node, "t1", [], "s1", ts,
                               previous_error="上次 write_file 超时")

        user_msg = captured["messages"][-1]["content"]
        assert "上次 write_file 超时" in user_msg, user_msg
        assert "不要原样重复" in user_msg


class TestExecuteNodesThreadsPreviousError:
    """**接线测试**：失败原因必须在 `execute_nodes` 那一层被取出并传给 Executor。

    直接调 `Executor.execute(previous_error=...)` 只能证明"给了就生效"，证明不了
    "真的会给"——`NodeState.error` 由 evaluator 写入 `exec_state`，而把两边的信息接
    起来的那一行在 `execution.execute_nodes`。反证时正是这里漏了网。
    """

    @pytest.mark.asyncio
    async def test_second_attempt_receives_recorded_error(self):
        from Module.workflow_engine.core.execution import execute_nodes
        from Module.workflow_engine.schema import (
            ArtifactId,
            DAGNode,
            ExecutionPhase,
            ExecutionState,
            NodeResult,
            NodeState,
            NodeStatus,
            OutputContract,
        )

        node = DAGNode(
            node_id=NodeId("n1"), description="做事",
            output_contract=OutputContract(description="产出"),
            output_as=ArtifactId("output:n1"),
        )
        ns = NodeState(node_id=NodeId("n1"), status=NodeStatus.PENDING, attempt=1,
                       error="上次超时")
        exec_state = ExecutionState(
            node_states={NodeId("n1"): ns},
            current_phase=ExecutionPhase.EXECUTING,
        )

        seen: list[object] = []

        async def _exec(node, task_id, tools, session_id, ts, previous_error=None):
            seen.append(previous_error)
            return NodeResult(node_id=NodeId("n1"), artifact_refs=[], attempt=0,
                              duration_ms=1)

        executor = MagicMock()
        executor.execute = AsyncMock(side_effect=_exec)

        await execute_nodes(executor, exec_state, [node], "t1", [], "s1", MagicMock())

        assert ns.attempt == 2, "attempt 已自增到第二轮"
        assert seen == ["上次超时"], (
            "第二轮必须把上一轮记录的 error 传下去——否则重试与首次逐字节相同（盲重跑）")
