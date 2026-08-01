"""Orchestrator 测试——单元测试（mock Executor/Evaluator）+ 集成测试（run() 主循环）。

覆盖：_apply_evaluation 全部分支、RETRY 上限、_finalize 统计、主循环各种退出路径。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from Module.workflow_engine.core.graph import PlanGraph
from Module.workflow_engine.core.orchestrator import Orchestrator
from Module.workflow_contract import (
    ConstraintConfig,
    NodeId,
    WorkflowStatus,
    WorkflowId,
)
from Module.tool.tool_descriptor import Tool
from Module.workflow_engine.schema import (
    ArtifactId,
    DAGNode,
    EvaluatorAction,
    ExecutionPhase,
    ExecutionState,
    ExecutionStatus,
    BusinessStatus,
    NodeErrorCode,
    NodeResult,
    NodeResultEvaluation,
    NodeState,
    NodeStatus,
    OrchestratorResult,
    OrchestratorRunStatus,
    WorkflowErrorCode,
    WorkflowState,
    Plan,
)

pytestmark = pytest.mark.unit

# ── 测试固件 ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_infra():
    """Mock AgentInfra——提供 constraints。"""
    infra = MagicMock()
    infra.constraints = ConstraintConfig(max_retries_per_node=3)
    return infra


@pytest.fixture
def mock_executor():
    """Mock Executor——根据节点 node_id 返回成功结果。"""
    ex = MagicMock()

    async def _exec(node, task_id, tools, session_id=""):
        return NodeResult(
            node_id=node.node_id,
            artifact_refs=[],
            error=None,
            attempt=1,
            duration_ms=100,
        )
    ex.execute = AsyncMock(side_effect=_exec)
    return ex


@pytest.fixture
def mock_evaluator():
    """Mock Evaluator——根据输入 result.node_id 返回 CONTINUE。"""
    ev = MagicMock()

    async def _eval(result, node, plan):
        return NodeResultEvaluation(
            node_id=result.node_id,
            execution_status=ExecutionStatus.OK,
            business_status=BusinessStatus.GOAL_ACHIEVED,
            reason="成功",
            suggested_action=EvaluatorAction.CONTINUE,
            result=result,
        )
    ev.evaluate = AsyncMock(side_effect=_eval)
    return ev


@pytest.fixture
def mock_hitl():
    return MagicMock()


@pytest.fixture
def mock_checkpoint():
    """Mock CheckpointManager——save/load 均为 async。"""
    mgr = MagicMock()
    mgr.save = AsyncMock()
    mgr.load = AsyncMock()
    return mgr


@pytest.fixture
def mock_artifact_store():
    return MagicMock()


@pytest.fixture
def orchestrator(mock_infra, mock_executor, mock_evaluator, mock_hitl, mock_checkpoint, mock_artifact_store):
    """创建被测 Orchestrator 实例。"""
    return Orchestrator(
        infra=mock_infra,
        hitl_notifier=mock_hitl,
        executor=mock_executor,
        evaluator=mock_evaluator,
        checkpoint_manager=mock_checkpoint,
        artifact_store=mock_artifact_store,
    )


@pytest.fixture
def mock_mailbox():
    """Mock WorkflowMailbox。"""
    return MagicMock()


def _make_graph(nodes: list[str], edges: list[tuple[str, str]]) -> PlanGraph:
    """快速构建 PlanGraph（所有节点 required=True）。"""
    from Module.workflow_engine.schema import (
        DAG,
        Plan,
    )
    dag_nodes = [DAGNode(node_id=NodeId(n)) for n in nodes]
    plan = Plan(dag=DAG(
        nodes=dag_nodes,
        edges=[(NodeId(f), NodeId(t)) for f, t in edges],
    ), plan_version=1)
    return PlanGraph.from_plan(plan)


def _task_state(task_id: str = "t1", graph: PlanGraph | None = None) -> WorkflowState:
    """创建 WorkflowState——可选从 graph 导出 Plan。status=RUNNING（Orchestrator 入口状态）。"""
    plan = graph.to_plan() if graph else None
    return WorkflowState(
        task_id=WorkflowId(task_id),
        status=WorkflowStatus.RUNNING,
        plan=plan,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


# ── 单元测试：_init_exec_state ──────────────────────────────────────────────


def test_init_exec_state(orchestrator):
    """初始 ExecutionState 所有节点 PENDING。"""
    graph = _make_graph(["a", "b", "c"], [("a", "b"), ("b", "c")])
    es = orchestrator._init_exec_state(graph)

    assert len(es.node_states) == 3
    for nid in (NodeId("a"), NodeId("b"), NodeId("c")):
        assert es.node_states[nid].status == NodeStatus.PENDING
    assert es.current_phase == ExecutionPhase.EXECUTING


# ── 单元测试：_apply_evaluation ─────────────────────────────────────────────


def _make_eval(node_id: str, action: EvaluatorAction) -> NodeResultEvaluation:
    return NodeResultEvaluation(
        node_id=NodeId(node_id),
        execution_status=ExecutionStatus.OK,
        business_status=BusinessStatus.GOAL_ACHIEVED,
        reason="test",
        suggested_action=action,
        result=NodeResult(node_id=NodeId(node_id), error=None, attempt=1, duration_ms=100),
    )


@pytest.mark.asyncio
async def test_apply_continue(orchestrator, mock_mailbox):
    """CONTINUE → SUCCESS, push progress。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)

    action = await orchestrator._apply_evaluation(
        ts, es, _make_eval("a", EvaluatorAction.CONTINUE), mock_mailbox, graph,
    )

    assert action == EvaluatorAction.CONTINUE
    assert es.node_states[NodeId("a")].status == NodeStatus.SUCCESS


@pytest.mark.asyncio
async def test_apply_skip(orchestrator, mock_mailbox):
    """SKIP → SKIPPED（仅非必需节点）。"""
    graph = PlanGraph.from_plan(_make_plan_with_node(NodeId("a"), required=False))
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)

    action = await orchestrator._apply_evaluation(
        ts, es, _make_eval("a", EvaluatorAction.SKIP), mock_mailbox, graph,
    )

    assert action == EvaluatorAction.SKIP
    assert es.node_states[NodeId("a")].status == NodeStatus.SKIPPED


@pytest.mark.asyncio
async def test_apply_skip_required_demoted_to_replan(orchestrator, mock_mailbox):
    """required 节点收到 SKIP → 降级为 REPLAN（防御死锁）。"""
    graph = _make_graph(["a"], [])  # required=True
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)

    action = await orchestrator._apply_evaluation(
        ts, es, _make_eval("a", EvaluatorAction.SKIP), mock_mailbox, graph,
    )

    assert action == EvaluatorAction.REPLAN
    assert es.node_states[NodeId("a")].status == NodeStatus.FAILED


@pytest.mark.asyncio
async def test_apply_retry(orchestrator, mock_mailbox):
    """RETRY → PENDING（未超上限）。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)
    # 设置 attempt=1（还没到上限 3）
    es.node_states[NodeId("a")].attempt = 1

    action = await orchestrator._apply_evaluation(
        ts, es, _make_eval("a", EvaluatorAction.RETRY), mock_mailbox, graph,
    )

    assert action == EvaluatorAction.RETRY
    assert es.node_states[NodeId("a")].status == NodeStatus.PENDING


@pytest.mark.asyncio
async def test_apply_retry_exceeded(orchestrator, mock_mailbox):
    """RETRY 超上限 → FAILED + return REPLAN。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)
    # attempt > max_retries(3) 触发耗尽——即第 4 次尝试（初始 + 3 次重试）
    es.node_states[NodeId("a")].attempt = 4

    action = await orchestrator._apply_evaluation(
        ts, es, _make_eval("a", EvaluatorAction.RETRY), mock_mailbox, graph,
    )

    assert action == EvaluatorAction.REPLAN
    assert es.node_states[NodeId("a")].status == NodeStatus.FAILED
    assert "已达上限" in es.node_states[NodeId("a")].error


@pytest.mark.asyncio
async def test_apply_retry_node_max_retries(orchestrator, mock_mailbox):
    """节点级 max_retries 覆盖全局。"""
    graph = PlanGraph.from_plan(_make_plan_with_node(NodeId("a"), max_retries=2))
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)
    es.node_states[NodeId("a")].attempt = 3  # > max_retries(2) 触发耗尽

    action = await orchestrator._apply_evaluation(
        ts, es, _make_eval("a", EvaluatorAction.RETRY), mock_mailbox, graph,
    )

    assert action == EvaluatorAction.REPLAN  # 节点上限 2，attempt=2 触发


def _make_plan_with_node(nid: NodeId, **kwargs) -> Plan:
    from Module.workflow_engine.schema import (
        DAG,
        Plan,
    )
    return Plan(dag=DAG(
        nodes=[DAGNode(node_id=nid, **kwargs)],
        edges=[],
    ), plan_version=1)


@pytest.mark.asyncio
async def test_apply_replan(orchestrator, mock_mailbox):
    """REPLAN → FAILED, return REPLAN。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)

    action = await orchestrator._apply_evaluation(
        ts, es, _make_eval("a", EvaluatorAction.REPLAN), mock_mailbox, graph,
    )

    assert action == EvaluatorAction.REPLAN
    assert es.node_states[NodeId("a")].status == NodeStatus.FAILED


@pytest.mark.asyncio
async def test_apply_ask_approval(orchestrator, mock_mailbox):
    """ASK_APPROVAL → WAITING_APPROVAL + return ASK_APPROVAL。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)

    action = await orchestrator._apply_evaluation(
        ts, es, _make_eval("a", EvaluatorAction.ASK_APPROVAL), mock_mailbox, graph,
    )

    assert action == EvaluatorAction.ASK_APPROVAL
    assert es.waiting_for_approval is True
    assert es.node_states[NodeId("a")].status == NodeStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_apply_abort(orchestrator, mock_mailbox):
    """ABORT → FAILED, return ABORT。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)

    action = await orchestrator._apply_evaluation(
        ts, es, _make_eval("a", EvaluatorAction.ABORT), mock_mailbox, graph,
    )

    assert action == EvaluatorAction.ABORT
    assert es.node_states[NodeId("a")].status == NodeStatus.FAILED


# ── 单元测试：_finalize ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_counts_skipped(orchestrator, mock_mailbox):
    """_finalize 计数时包含 SKIPPED 节点，Mailbox 推送含正确统计。"""
    graph = _make_graph(["a", "b", "c"], [])
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)
    es.node_states[NodeId("a")].status = NodeStatus.SUCCESS
    es.node_states[NodeId("b")].status = NodeStatus.SKIPPED
    es.node_states[NodeId("c")].status = NodeStatus.FAILED

    await orchestrator._finalize(ts, es, mock_mailbox)

    assert ts.status == WorkflowStatus.COMPLETED

    # 验证 Mailbox 推送了 completed 事件，且计数正确
    push_calls = [c for c in mock_mailbox._push.call_args_list
                  if c[0][0].type.value == "completed"]
    assert len(push_calls) == 1, "应推送一次 completed 事件"
    data = push_calls[0][0][0].data
    assert data["task_id"] == str(ts.task_id)
    assert data["total_nodes"] == 3
    assert data["completed_nodes"] == 2        # SUCCESS + SKIPPED
    assert data["failed_nodes"] == 1           # FAILED
    assert "2/3" in data["result"]
    assert "1 个失败" in data["result"]
    assert "成功 2" in data["summary"]
    assert "失败 1" in data["summary"]


# ── 单元测试：_needs_approval ──────────────────────────────────────────────


def test_needs_approval_always_false(orchestrator):
    """v1 _needs_approval 始终返回 False。"""
    node = DAGNode(node_id=NodeId("x"))
    assert orchestrator._needs_approval(node) is False


# ── 集成测试：run() 主循环 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_success(orchestrator, mock_mailbox):
    """A→B 链，两节点都成功 → SUCCESS。"""
    graph = _make_graph(["a", "b"], [("a", "b")])
    ts = _task_state(graph=graph)

    result = await orchestrator.run(ts, graph, mock_mailbox)

    assert result.status == OrchestratorRunStatus.SUCCESS
    assert ts.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_run_needs_replan(orchestrator, mock_mailbox, mock_evaluator):
    """Evaluator 返回 REPLAN → NEEDS_REPLAN。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)

    mock_evaluator.evaluate = AsyncMock(return_value=NodeResultEvaluation(
        node_id=NodeId("a"),
        execution_status=ExecutionStatus.ERROR,
        business_status=BusinessStatus.GOAL_FAILED,
        reason="工具匹配失败",
        suggested_action=EvaluatorAction.REPLAN,
        result=NodeResult(node_id=NodeId("a"), error=NodeErrorCode.TOOL_NOT_FOUND, attempt=1, duration_ms=100),
    ))

    result = await orchestrator.run(ts, graph, mock_mailbox)

    assert result.status == OrchestratorRunStatus.NEEDS_REPLAN
    assert "工具匹配失败" in result.error_context.get("reason", "")


@pytest.mark.asyncio
async def test_run_abort(orchestrator, mock_mailbox, mock_evaluator):
    """Evaluator 返回 ABORT → FAILED。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)

    mock_evaluator.evaluate = AsyncMock(return_value=NodeResultEvaluation(
        node_id=NodeId("a"),
        execution_status=ExecutionStatus.PERMISSION_DENIED,
        business_status=BusinessStatus.GOAL_FAILED,
        reason="用户拒绝审批",
        suggested_action=EvaluatorAction.ABORT,
        result=NodeResult(node_id=NodeId("a"), error=NodeErrorCode.PERMISSION_DENIED, attempt=1, duration_ms=100),
    ))

    result = await orchestrator.run(ts, graph, mock_mailbox)

    assert result.status == OrchestratorRunStatus.FAILED


@pytest.mark.asyncio
async def test_run_pause_requested(orchestrator, mock_mailbox):
    """exec_state.pause_requested → PAUSED。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)

    # 注入 pause_requested——需要 monkeypatch _init_exec_state 或直接构造 exec_state
    # 因为 _init_exec_state 每次 run() 都会重建，我们通过 subclass 覆写
    original_init = orchestrator._init_exec_state

    def _patched_init(g):
        es = original_init(g)
        es.pause_requested = True
        return es
    orchestrator._init_exec_state = _patched_init

    result = await orchestrator.run(ts, graph, mock_mailbox)

    assert result.status == OrchestratorRunStatus.PAUSED
    assert ts.status == WorkflowStatus.PAUSED


@pytest.mark.asyncio
async def test_run_blocked(orchestrator, mock_mailbox, mock_evaluator):
    """Evaluator 返回 ASK_APPROVAL → BLOCKED。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)

    mock_evaluator.evaluate = AsyncMock(return_value=NodeResultEvaluation(
        node_id=NodeId("a"),
        execution_status=ExecutionStatus.OK,
        business_status=BusinessStatus.GOAL_ACHIEVED,
        reason="需要用户批准",
        suggested_action=EvaluatorAction.ASK_APPROVAL,
        result=NodeResult(node_id=NodeId("a"), error=None, attempt=1, duration_ms=100),
    ))

    result = await orchestrator.run(ts, graph, mock_mailbox)

    assert result.status == OrchestratorRunStatus.BLOCKED
    assert ts.status == WorkflowStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_run_retry_then_success(orchestrator, mock_mailbox, mock_evaluator):
    """第一次 RETRY，第二次 CONTINUE → 最终 SUCCESS。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)

    call_count = 0

    async def _flaky_evaluate(result, node, plan):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return NodeResultEvaluation(
                node_id=NodeId("a"),
                execution_status=ExecutionStatus.ERROR,
                business_status=BusinessStatus.GOAL_FAILED,
                reason="临时错误",
                suggested_action=EvaluatorAction.RETRY,
                result=result,
            )
        else:
            return NodeResultEvaluation(
                node_id=NodeId("a"),
                execution_status=ExecutionStatus.OK,
                business_status=BusinessStatus.GOAL_ACHIEVED,
                reason="重试成功",
                suggested_action=EvaluatorAction.CONTINUE,
                result=result,
            )

    mock_evaluator.evaluate = AsyncMock(side_effect=_flaky_evaluate)

    result = await orchestrator.run(ts, graph, mock_mailbox)

    assert result.status == OrchestratorRunStatus.SUCCESS
    assert call_count == 2  # 一次失败 + 一次成功


@pytest.mark.asyncio
async def test_run_executor_error_retry(orchestrator, mock_mailbox, mock_executor, mock_evaluator):
    """Executor 返回错误 → Evaluator 判 RETRY → 第二次成功。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)

    exec_count = 0

    async def _flaky_exec(node, tid, tools, session_id=""):
        nonlocal exec_count
        exec_count += 1
        if exec_count == 1:
            return NodeResult(node_id=NodeId("a"), error=NodeErrorCode.TOOL_TIMEOUT, attempt=1, duration_ms=5000)
        else:
            return NodeResult(node_id=NodeId("a"), error=None, attempt=2, duration_ms=100)

    mock_executor.execute = AsyncMock(side_effect=_flaky_exec)
    # Evaluator 适配：timeout → retry, no error → continue
    async def _eval(result, node, plan):
        if result.error:
            return NodeResultEvaluation(
                node_id=NodeId("a"),
                execution_status=ExecutionStatus.TIMEOUT,
                business_status=BusinessStatus.GOAL_FAILED,
                reason="超时",
                suggested_action=EvaluatorAction.RETRY,
                result=result,
            )
        return NodeResultEvaluation(
            node_id=NodeId("a"),
            execution_status=ExecutionStatus.OK,
            business_status=BusinessStatus.GOAL_ACHIEVED,
            reason="成功",
            suggested_action=EvaluatorAction.CONTINUE,
            result=result,
        )
    mock_evaluator.evaluate = AsyncMock(side_effect=_eval)

    result = await orchestrator.run(ts, graph, mock_mailbox)

    assert result.status == OrchestratorRunStatus.SUCCESS
    assert exec_count == 2


@pytest.mark.asyncio
async def test_run_failed_required_triggers_replan(orchestrator, mock_mailbox):
    """required 节点 FAILED → has_failed_required → NEEDS_REPLAN。"""
    graph = _make_graph(["a", "b"], [("a", "b")])
    ts = _task_state(graph=graph)

    original_init = orchestrator._init_exec_state
    def _patched_init(g):
        es = original_init(g)
        es.node_states[NodeId("a")].status = NodeStatus.FAILED
        return es
    orchestrator._init_exec_state = _patched_init

    result = await orchestrator.run(ts, graph, mock_mailbox)
    assert result.status == OrchestratorRunStatus.NEEDS_REPLAN


@pytest.mark.asyncio
async def test_run_deadlock(orchestrator, mock_mailbox):
    """真正的死锁：三个 required 节点全部 SKIPPED → 无 ready + 非 terminal + 无 blocked + 无 failed → FAILED。

    构造：patch _init_exec_state 注入全 SKIPPED 状态，_apply_evaluation 不会被调用（无 ready 节点）。
    """
    graph = _make_graph(["a", "b", "c"], [])
    ts = _task_state(graph=graph)

    original_init = orchestrator._init_exec_state
    def _patched_init(g):
        es = original_init(g)
        for nid in (NodeId("a"), NodeId("b"), NodeId("c")):
            es.node_states[nid].status = NodeStatus.SKIPPED
        return es
    orchestrator._init_exec_state = _patched_init

    result = await orchestrator.run(ts, graph, mock_mailbox)

    # get_ready_nodes 找不到 PENDING 节点（全 SKIPPED）
    # is_terminal 返回 False（required 节点不是 SUCCESS）
    # has_failed_required 返回 False（没有 FAILED/UNRECOVERABLE）
    # → 死锁 → FAILED
    assert result.status == OrchestratorRunStatus.FAILED
    assert "死锁" in result.error_context.get("reason", "")


# ── 补充测试：异常路径 + Plan=None + checkpoint/artifact ──────────────────


@pytest.mark.asyncio
async def test_run_executor_exception(orchestrator, mock_mailbox, mock_executor):
    """Executor 抛异常 → except Exception → FAILED + INTERNAL_ERROR。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)
    mock_executor.execute = AsyncMock(side_effect=RuntimeError("Executor 崩溃"))

    result = await orchestrator.run(ts, graph, mock_mailbox)

    assert result.status == OrchestratorRunStatus.FAILED
    # Mailbox 推送了 failed 事件
    failed_calls = [c for c in mock_mailbox._push.call_args_list
                    if c[0][0].type.value == "failed"]
    assert len(failed_calls) == 1
    assert "内部异常" in failed_calls[0][0][0].data["error"]


@pytest.mark.asyncio
async def test_evaluate_node_plan_none_abort(orchestrator, mock_mailbox):
    """_evaluate_node 遇 plan=None → ABORT + execution_status=ERROR。"""
    graph = _make_graph(["a"], [])
    ts = _task_state()  # 不传 graph，plan=None

    evaluation = await orchestrator._evaluate_node(
        NodeResult(node_id=NodeId("a"), error=None, attempt=1, duration_ms=100),
        DAGNode(node_id=NodeId("a")),
        ts,
    )

    assert evaluation.suggested_action == EvaluatorAction.ABORT
    assert evaluation.execution_status == ExecutionStatus.ERROR
    assert "Plan 缺失" in evaluation.reason


def test_build_checkpoint(orchestrator):
    """_build_checkpoint 返回 WorkflowCheckpoint（task + exec 类型化）。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)

    cp = orchestrator._build_checkpoint(ts, es)

    assert cp.task.task_id == WorkflowId("t1")
    assert "a" in cp.exec.node_states


@pytest.mark.asyncio
async def test_save_checkpoint_calls_mgr(orchestrator, mock_checkpoint):
    """_save_checkpoint 调用 checkpoint_mgr.save(WorkflowCheckpoint)。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)

    await orchestrator._save_checkpoint(ts, es)

    mock_checkpoint.save.assert_called_once()
    args = mock_checkpoint.save.call_args
    assert args[0][0] == WorkflowId("t1")         # task_id
    assert args[0][1].task.task_id == WorkflowId("t1")  # WorkflowCheckpoint.task


@pytest.mark.asyncio
async def test_apply_continue_copies_artifact_refs(orchestrator, mock_mailbox):
    """CONTINUE 时 artifact_refs 被复制到 NodeState。"""
    graph = _make_graph(["a"], [])
    ts = _task_state(graph=graph)
    es = orchestrator._init_exec_state(graph)

    eval_with_artifacts = NodeResultEvaluation(
        node_id=NodeId("a"),
        execution_status=ExecutionStatus.OK,
        business_status=BusinessStatus.GOAL_ACHIEVED,
        reason="ok",
        suggested_action=EvaluatorAction.CONTINUE,
        result=NodeResult(
            node_id=NodeId("a"),
            artifact_refs=[ArtifactId("art_1"), ArtifactId("art_2")],
            error=None, attempt=1, duration_ms=100,
        ),
    )

    await orchestrator._apply_evaluation(
        ts, es, eval_with_artifacts, mock_mailbox, graph,
    )

    assert es.node_states[NodeId("a")].artifact_refs == [ArtifactId("art_1"), ArtifactId("art_2")]
