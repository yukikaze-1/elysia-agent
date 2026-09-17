"""SubAgent 单元测试。

覆盖 SubAgent 决策层所有路径：
    - run() 主流程（成功 / 校验 abort）
    - _execute() / _dispatch_result() 5 条 Orchestrator 返回路径
    - _check_meltdown() 熔断判断
    - _extract_node_results() 从 checkpoint 提取节点状态
    - _abort_error_code() 校验问题 → 错误码映射

SubAgent.__init__ 签名为 (infra, hitl_notifier) —— 组件在 run() 内创建。
测试通过直接赋值 agent._planner / agent._orchestrator 等 mock 属性来注入依赖。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Module.workflow_engine.core.sub_agent import SubAgent
from Module.workflow_contract import AgentInfra
from Module.workflow_engine.mailbox import WorkflowMailbox
from Module.workflow_contract import (
    ConstraintConfig,
    DelegationContext,
    NodeId,
    SessionId,
    WorkflowEventType,
    WorkflowId,
    WorkflowStatus,
)
from Module.workflow_engine.schema import (
    ArtifactId,
    DAG,
    DAGNode,
    DAGPatch,
    DAGPatchType,
    ExecutionState,
    NodeState,
    NodeStatus,
    OrchestratorResult,
    OrchestratorRunStatus,
    Plan,
    WorkflowCheckpoint,
    WorkflowErrorCode,
    WorkflowState,
    ValidationAction,
    ValidationIssue,
    ValidationIssueType,
    ValidationResult,
    now_iso,
)

pytestmark = pytest.mark.unit

# ── 工厂函数 ────────────────────────────────────────────────────────────────


def _task_state(task_id: str = "t1") -> WorkflowState:
    """最小 WorkflowState。"""
    return WorkflowState(
        task_id=WorkflowId(task_id),
        status=WorkflowStatus.RUNNING,
        plan=Plan(dag=DAG(nodes=[], edges=[]), plan_version=1),
        created_at=now_iso(),
        updated_at=now_iso(),
    )


def _delegation_context() -> DelegationContext:
    """最小 DelegationContext。"""
    return DelegationContext(
        intent="测试任务",
        session_id=SessionId("s1"),
    )


def _checkpoint(task_id: str = "t1") -> WorkflowCheckpoint:
    """最小 WorkflowCheckpoint（含一个 NodeState）。"""
    ts = _task_state(task_id)
    es = ExecutionState(
        node_states={
            NodeId("a"): NodeState(
                node_id=NodeId("a"),
                status=NodeStatus.SUCCESS,
                summary="节点 a 完成",
                error=None,
            ),
        },
    )
    return WorkflowCheckpoint(task=ts, exec=es)


def _mock_plan() -> Plan:
    """最小合法 Plan。"""
    return Plan(
        dag=DAG(nodes=[DAGNode(node_id=NodeId("a"), description="步骤 a")]),
        plan_version=1,
    )


# ── SubAgent 构造辅助 ───────────────────────────────────────────────────────


def _make_sub_agent(
    *,
    infra=None,
    hitl=None,
):
    """构造 SubAgent（新 2-arg 签名）。

    返回 agent 实例。调用方需自行赋值 mock 到 agent._planner、
    agent._orchestrator 等属性，再调用被测方法。
    """
    from Module.workflow_engine.core.sub_agent import SubAgent

    if infra is None:
        infra = MagicMock()
        infra.constraints = ConstraintConfig(max_failure_count=5)
    if hitl is None:
        hitl = MagicMock()

    agent = SubAgent(infra=infra, hitl_notifier=hitl)
    return agent


def _assign_execute_mocks(agent, *, mock_orch=None):
    """为 _execute() 路径测试预赋值 mock 组件到 agent。

    _execute() 内部使用的组件：
        - _orchestrator.run()
        - _replanner.replan()（_handle_replan）
        - _plan_validator.validate()（_handle_replan 全量重建）
        - _checkpoint_mgr.save()（_handle_paused）
        - _infra.constraints（_check_meltdown）
    """
    if mock_orch is None:
        mock_orch = MagicMock()
        mock_orch.run = AsyncMock()
    agent._orchestrator = mock_orch

    # 默认 mock planner / replanner / validator / checkpoint_mgr ——
    # 各测试按需覆盖具体行为
    if agent._planner is None:
        agent._planner = MagicMock()
        agent._planner.plan = AsyncMock()
    if agent._replanner is None:
        agent._replanner = MagicMock()
        agent._replanner.replan = AsyncMock()
    if agent._plan_validator is None:
        agent._plan_validator = MagicMock()
    if agent._checkpoint_mgr is None:
        agent._checkpoint_mgr = MagicMock()
        agent._checkpoint_mgr.save = AsyncMock()

    return mock_orch


# ── run() 测试辅助：patch 所有 run() 内部创建的组件 ─────────────────────────

# run() 内部通过 `from X import Y` 创建组件实例，
# patch 需要打到源模块上（`from X import Y` 从 X.__dict__ 取 Y）
_RUN_COMPONENT_PATCHES = [
    "Module.workflow_engine.core.planner.Planner",
    "Module.workflow_engine.core.plan_validator.PlanValidator",
    "Module.workflow_engine.core.orchestrator.Orchestrator",
    "Module.workflow_engine.core.executor.Executor",
    "Module.workflow_engine.core.evaluator.Evaluator",
    "Module.workflow_engine.checkpoint.CheckpointManager",
    "Module.workflow_engine.artifact_store.ArtifactStore",
    "Module.workflow_engine.token_ledger.TokenLedger",
]


def _make_run_test_agent():
    """为 run() 端到端测试构造 agent + mock 组件字典。

    Returns:
        agent, mocks dict with keys: planner, validator, orchestrator
    """
    from Module.workflow_engine.core.sub_agent import SubAgent

    infra = MagicMock()
    infra.constraints = ConstraintConfig(max_failure_count=5)
    hitl = MagicMock()

    agent = SubAgent(infra=infra, hitl_notifier=hitl)

    mock_planner = MagicMock()
    mock_planner.plan = AsyncMock(return_value=_mock_plan())
    mock_validator = MagicMock()
    mock_orch = MagicMock()
    mock_orch.run = AsyncMock()

    mocks = {
        "planner": mock_planner,
        "validator": mock_validator,
        "orchestrator": mock_orch,
    }

    return agent, mocks


# ── run() 主流程 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_success():
    """完整成功路径——规划 → 校验通过 → 执行完成。"""
    agent, mocks = _make_run_test_agent()
    mock_planner = mocks["planner"]
    mock_validator = mocks["validator"]
    mock_orch = mocks["orchestrator"]

    from Module.workflow_engine.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    mock_validator.validate.return_value = (
        ValidationResult(valid=True, action=ValidationAction.PROCEED),
        graph,
    )
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.SUCCESS,
    )

    mailbox = WorkflowMailbox(WorkflowId("t1"))

    # patch 所有 run() 内部创建的组件类
    with patch(_RUN_COMPONENT_PATCHES[0], return_value=mock_planner), \
         patch(_RUN_COMPONENT_PATCHES[1], return_value=mock_validator), \
         patch(_RUN_COMPONENT_PATCHES[2], return_value=mock_orch), \
         patch(_RUN_COMPONENT_PATCHES[3]), \
         patch(_RUN_COMPONENT_PATCHES[4]), \
         patch(_RUN_COMPONENT_PATCHES[5]), \
         patch(_RUN_COMPONENT_PATCHES[6]), \
         patch(_RUN_COMPONENT_PATCHES[7]):
        await agent.run(_delegation_context(), mailbox)

    # 验证 Planner 被调用
    mock_planner.plan.assert_called_once()
    # 验证 Orchestrator 被调用（task_state + graph + mailbox）
    mock_orch.run.assert_called_once()
    call_args = mock_orch.run.call_args[0]
    assert call_args[0].task_id == WorkflowId("t1")  # task_state
    assert call_args[1] is graph                 # graph


@pytest.mark.asyncio
async def test_run_validation_abort():
    """校验 ABORT → 任务 FAILED + 推送 FAILED 事件。"""
    agent, mocks = _make_run_test_agent()
    mock_planner = mocks["planner"]
    mock_validator = mocks["validator"]
    mock_orch = mocks["orchestrator"]

    issues = [
        ValidationIssue(
            type=ValidationIssueType.MAX_NODES_EXCEEDED,
            message="DAG 节点数 25 超过上限 20",
        )
    ]
    mock_validator.validate.return_value = (
        ValidationResult(valid=False, issues=issues, action=ValidationAction.ABORT),
        MagicMock(),  # graph——校验失败但 graph 仍返回
    )

    mailbox = WorkflowMailbox(WorkflowId("t1"))

    with patch(_RUN_COMPONENT_PATCHES[0], return_value=mock_planner), \
         patch(_RUN_COMPONENT_PATCHES[1], return_value=mock_validator), \
         patch(_RUN_COMPONENT_PATCHES[2], return_value=mock_orch), \
         patch(_RUN_COMPONENT_PATCHES[3]), \
         patch(_RUN_COMPONENT_PATCHES[4]), \
         patch(_RUN_COMPONENT_PATCHES[5]), \
         patch(_RUN_COMPONENT_PATCHES[6]), \
         patch(_RUN_COMPONENT_PATCHES[7]):
        await agent.run(_delegation_context(), mailbox)

    # 不应调用 Orchestrator——校验失败直接返回
    mock_orch.run.assert_not_called()

    # 验证 FAILED 事件推送到 mailbox
    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == WorkflowEventType.FAILED
    assert "plan_too_large" in events[0].data.get("error_type", "")


# ── _execute() / _dispatch_result() 路径 ────────────────────────────────────


@pytest.mark.asyncio
async def test_run_loop_success():
    """Orchestrator 返回 SUCCESS → 循环结束。"""
    agent = _make_sub_agent()
    mock_orch = _assign_execute_mocks(agent)
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.SUCCESS,
    )

    from Module.workflow_engine.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())

    await agent._execute(_task_state(), graph, WorkflowMailbox(WorkflowId("t1")), _delegation_context())

    mock_orch.run.assert_called_once()


@pytest.mark.asyncio
async def test_run_loop_blocked():
    """Orchestrator 返回 BLOCKED → 推送 APPROVAL_NEEDED → 返回。"""
    agent = _make_sub_agent()
    mock_orch = _assign_execute_mocks(agent)
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.BLOCKED,
        checkpoint_state=_checkpoint("t1"),
    )

    from Module.workflow_engine.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    mailbox = WorkflowMailbox(WorkflowId("t1"))

    await agent._execute(_task_state(), graph, mailbox, _delegation_context())

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == WorkflowEventType.APPROVAL_NEEDED


@pytest.mark.asyncio
async def test_run_loop_paused():
    """Orchestrator 返回 PAUSED → 保存 checkpoint + 推送 PAUSED → 返回。"""
    agent = _make_sub_agent()
    mock_orch = _assign_execute_mocks(agent)
    cp = _checkpoint("t1")
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.PAUSED,
        checkpoint_state=cp,
    )

    from Module.workflow_engine.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    mailbox = WorkflowMailbox(WorkflowId("t1"))

    await agent._execute(_task_state(), graph, mailbox, _delegation_context())

    # 验证 checkpoint 被保存
    assert agent._checkpoint_mgr is not None
    agent._checkpoint_mgr.save.assert_called_once_with(WorkflowId("t1"), cp)  # type: ignore[reportAttributeAccessIssue]

    # 验证 PAUSED 事件
    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == WorkflowEventType.PAUSED


@pytest.mark.asyncio
async def test_run_loop_failed():
    """Orchestrator 返回 FAILED → 推送 FAILED → 返回。"""
    agent = _make_sub_agent()
    mock_orch = _assign_execute_mocks(agent)
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.FAILED,
        error_context={"reason": "检测到死锁"},
    )

    from Module.workflow_engine.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    mailbox = WorkflowMailbox(WorkflowId("t1"))

    await agent._execute(_task_state(), graph, mailbox, _delegation_context())

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == WorkflowEventType.FAILED
    assert "检测到死锁" in events[0].data["error"]


@pytest.mark.asyncio
async def test_run_loop_needs_replan_meltdown():
    """Orchestrator 返回 NEEDS_REPLAN + 达到熔断上限 → FAILED。"""
    agent = _make_sub_agent()
    mock_orch = _assign_execute_mocks(agent)
    # 设置极低的熔断上限
    agent._infra.constraints = ConstraintConfig(max_failure_count=1)
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.NEEDS_REPLAN,
        error_context={"failed_nodes": [NodeId("a")], "reason": "节点失败"},
        checkpoint_state=_checkpoint("t1"),
    )

    from Module.workflow_engine.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    # plan_version > max_failure_count → 熔断
    ts = _task_state()
    ts.plan = Plan(dag=DAG(nodes=[], edges=[]), plan_version=2)  # 2 > 1

    mailbox = WorkflowMailbox(WorkflowId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == WorkflowEventType.FAILED
    assert "failure_meltdown" in events[0].data.get("error_type", "")


# ── _handle_replan 路径 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_replan_dagpatch_success():
    """replan 返回 DAGPatch → apply_patch + version += 1 + 返回 (True, resume_state)。"""
    agent = _make_sub_agent()
    mock_orch = _assign_execute_mocks(agent)

    patch = DAGPatch(
        patch_type=DAGPatchType.ADD_NODES,
        reason="需要前置步骤获取时间",
        add_nodes=[
            DAGNode(node_id=NodeId("get_time"), description="获取当前时间",
                    output_as=ArtifactId("output:get_time"))
        ],
        add_edges=[(NodeId("get_time"), NodeId("a"))],
    )
    assert agent._replanner is not None
    agent._replanner.replan = AsyncMock(return_value=patch)

    # 第一次 run() 返回 NEEDS_REPLAN，第二次返回 SUCCESS
    mock_orch.run = AsyncMock(side_effect=[
        OrchestratorResult(
            status=OrchestratorRunStatus.NEEDS_REPLAN,
            error_context={"failed_nodes": [NodeId("a")], "reason": "工具超时"},
            checkpoint_state=_checkpoint("t1"),
        ),
        OrchestratorResult(status=OrchestratorRunStatus.SUCCESS),
    ])

    from Module.workflow_engine.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    ts = _task_state()
    ts.plan = graph.to_plan(_mock_plan())
    ts.plan.plan_version = 3

    mailbox = WorkflowMailbox(WorkflowId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    # 验证 DAGPatch 已应用
    assert graph.node_count == 2  # 原 1 个节点 + 1 个新增
    assert NodeId("get_time") in graph.nodes

    # 版本号正确递增
    assert ts.plan.plan_version == 4  # 3 + 1

    # 没有 FAILED 事件
    events = _collect_mailbox_events(mailbox)
    event_types = [e.type for e in events]
    assert WorkflowEventType.FAILED not in event_types

    # S2-D2：第二次 run() 必须拿到旧 exec_state（合并保留 SUCCESS），而不是 None
    second_call = mock_orch.run.await_args_list[1]
    assert second_call.kwargs.get("resume_from") is not None, (
        "全量重建必须带回旧 exec_state——传 None 会让已成功节点整图重跑")


@pytest.mark.asyncio
async def test_handle_replan_dagpatch_validation_failure():
    """replan 返回 DAGPatch 引入结构问题（如孤立节点）→ FAILED。"""
    agent = _make_sub_agent()
    mock_orch = _assign_execute_mocks(agent)

    # 构造一个会引入孤立节点的 patch（add_nodes 不加边，>1 节点时触发 ORPHAN）
    patch = DAGPatch(
        patch_type=DAGPatchType.ADD_NODES,
        reason="添加孤立节点",
        add_nodes=[
            DAGNode(node_id=NodeId("orphan"), description="无依赖无被依赖的节点")
        ],
    )
    assert agent._replanner is not None
    agent._replanner.replan = AsyncMock(return_value=patch)

    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.NEEDS_REPLAN,
        error_context={"failed_nodes": [NodeId("a")], "reason": "失败"},
        checkpoint_state=_checkpoint("t1"),
    )

    from Module.workflow_engine.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    ts = _task_state()
    ts.plan = graph.to_plan(_mock_plan())

    mailbox = WorkflowMailbox(WorkflowId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == WorkflowEventType.FAILED
    assert "DAGPatch 校验失败" in events[0].data["error"]


@pytest.mark.asyncio
async def test_handle_replan_full_replan_success():
    """replan 返回 Plan → 校验通过 → graph.replace() + version 继承 + **带回旧 exec_state**。

    S2-D2：`resume_from` 原先固定为 `None`（旧注释把这条写成了预期行为），于是
    全量重建后 `_init_exec_state` 生成**全 PENDING**，新 DAG 里 node_id 相同、旧态为
    SUCCESS 的节点**从头再跑一遍**——包括已产生副作用的节点。DAGPatch 分支一直返回
    `checkpoint.exec` 走合并保留 SUCCESS，两条分支行为不一致。
    """
    agent = _make_sub_agent()
    mock_orch = _assign_execute_mocks(agent)

    new_plan = Plan(
        dag=DAG(nodes=[
            DAGNode(node_id=NodeId("new_search"), description="新搜索方案",
                    output_as=ArtifactId("output:new_search")),
        ], edges=[]),
        plan_version=0,  # sentinel——应被 SubAgent 覆写
    )
    assert agent._replanner is not None
    agent._replanner.replan = AsyncMock(return_value=new_plan)

    # PlanValidator 返回 valid
    from Module.workflow_engine.core.graph import PlanGraph
    valid_graph = PlanGraph.from_plan(new_plan)
    assert agent._plan_validator is not None
    agent._plan_validator.validate = MagicMock(
        return_value=(ValidationResult(valid=True, action=ValidationAction.PROCEED), valid_graph)
    )

    # 第一次 run() 返回 NEEDS_REPLAN，第二次返回 SUCCESS
    mock_orch.run = AsyncMock(side_effect=[
        OrchestratorResult(
            status=OrchestratorRunStatus.NEEDS_REPLAN,
            error_context={"failed_nodes": [NodeId("a")], "reason": "DAG 结构根本错了"},
            checkpoint_state=_checkpoint("t1"),
        ),
        OrchestratorResult(status=OrchestratorRunStatus.SUCCESS),
    ])

    graph = PlanGraph.from_plan(_mock_plan())
    ts = _task_state()
    ts.plan = graph.to_plan(_mock_plan())
    old_version = 5
    ts.plan.plan_version = old_version

    mailbox = WorkflowMailbox(WorkflowId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    # 图已被替换
    assert graph.node_count == 1
    assert NodeId("new_search") in graph.nodes
    assert NodeId("a") not in graph.nodes  # 旧节点不存在

    # 版本号从旧 plan 继承 + 1
    assert ts.plan.plan_version == old_version + 1  # 5 + 1 = 6

    # 没有 FAILED 事件
    events = _collect_mailbox_events(mailbox)
    event_types = [e.type for e in events]
    assert WorkflowEventType.FAILED not in event_types

    # S2-D2：第二次 run() 必须拿到旧 exec_state（合并保留 SUCCESS），而不是 None
    second_call = mock_orch.run.await_args_list[1]
    assert second_call.kwargs.get("resume_from") is not None, (
        "全量重建必须带回旧 exec_state——传 None 会让已成功节点整图重跑")


@pytest.mark.asyncio
async def test_handle_replan_full_replan_validation_failure():
    """replan 返回 Plan 但校验不通过 → FAILED。"""
    agent = _make_sub_agent()
    mock_orch = _assign_execute_mocks(agent)

    new_plan = Plan(
        dag=DAG(nodes=[
            DAGNode(node_id=NodeId("bad"), description="缺少 output_contract 的 required 节点"),
        ], edges=[]),
    )
    assert agent._replanner is not None
    agent._replanner.replan = AsyncMock(return_value=new_plan)

    # PlanValidator 返回 invalid
    assert agent._plan_validator is not None
    agent._plan_validator.validate = MagicMock(
        return_value=(
            ValidationResult(
                valid=False,
                issues=[ValidationIssue(
                    type=ValidationIssueType.MISSING_CONTRACT,
                    node_id=NodeId("bad"),
                    message="缺少 output_contract",
                )],
            ),
            MagicMock(),  # graph
        )
    )

    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.NEEDS_REPLAN,
        error_context={"failed_nodes": [NodeId("a")], "reason": "失败"},
        checkpoint_state=_checkpoint("t1"),
    )

    from Module.workflow_engine.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    ts = _task_state()
    ts.plan = graph.to_plan(_mock_plan())

    mailbox = WorkflowMailbox(WorkflowId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == WorkflowEventType.FAILED
    assert "全量重建校验失败" in events[0].data["error"]


@pytest.mark.asyncio
async def test_handle_replan_checkpoint_none():
    """NEEDS_REPLAN 但 checkpoint_state=None → FAILED。"""
    agent = _make_sub_agent()
    mock_orch = _assign_execute_mocks(agent)

    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.NEEDS_REPLAN,
        error_context={"failed_nodes": [NodeId("a")], "reason": "失败"},
        checkpoint_state=None,  # 缺少快照
    )

    from Module.workflow_engine.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    ts = _task_state()
    ts.plan = graph.to_plan(_mock_plan())

    mailbox = WorkflowMailbox(WorkflowId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == WorkflowEventType.FAILED
    assert "checkpoint 缺失" in events[0].data["error"]


@pytest.mark.asyncio
async def test_handle_replan_not_implemented():
    """replan 抛出 NotImplementedError → 优雅降级 FAILED。"""
    agent = _make_sub_agent()
    mock_orch = _assign_execute_mocks(agent)
    assert agent._replanner is not None
    agent._replanner.replan = AsyncMock(side_effect=NotImplementedError("replan 未实现"))

    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.NEEDS_REPLAN,
        error_context={"failed_nodes": [NodeId("a")], "reason": "工具超时"},
        checkpoint_state=_checkpoint("t1"),
    )

    from Module.workflow_engine.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    ts = _task_state()
    ts.plan = graph.to_plan(_mock_plan())

    mailbox = WorkflowMailbox(WorkflowId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == WorkflowEventType.FAILED
    assert "Replan 调用异常" in events[0].data["error"]


# ── _check_meltdown ─────────────────────────────────────────────────────────


def test_check_meltdown_ok():
    """plan_version ≤ max_failure_count → 不触发熔断。"""
    agent = _make_sub_agent()
    agent._infra.constraints = ConstraintConfig(max_failure_count=5)

    ts = _task_state()
    assert ts.plan is not None
    ts.plan.plan_version = 5  # 5 ≤ 5，不触发

    assert agent._check_meltdown(ts) is None


def test_check_meltdown_triggers():
    """plan_version > max_failure_count → 触发熔断。"""
    agent = _make_sub_agent()
    agent._infra.constraints = ConstraintConfig(max_failure_count=3)

    ts = _task_state()
    assert ts.plan is not None
    ts.plan.plan_version = 4  # 4 > 3，触发

    reason = agent._check_meltdown(ts)
    assert reason is not None
    assert "熔断" in reason


def test_meltdown_time_budget():
    """任务超过 max_task_duration_s 时触发时间熔断。"""
    from datetime import datetime, timezone, timedelta
    from Module.workflow_engine.core.sub_agent.replan import check_meltdown

    old_time = (datetime.now(timezone.utc) - timedelta(seconds=601)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reason = check_meltdown(
        plan_version=1, max_failure_count=5,
        max_task_duration_s=600.0, created_at=old_time,
        token_ledger=None, max_total_tokens=100_000,
    )
    assert reason is not None
    assert "600" in reason


def test_meltdown_time_fail_closed_on_malformed_created_at():
    """created_at 畸形时 fail-closed——按已超时处理，时间熔断必须触发（S4）。

    解析失败若静默返回 0.0，任务将失去时间上限保护，可无限烧资源。
    """
    from Module.workflow_engine.core.sub_agent.replan import check_meltdown

    reason = check_meltdown(
        plan_version=1, max_failure_count=5,
        max_task_duration_s=600.0, created_at="not-a-timestamp",
        token_ledger=None, max_total_tokens=100_000,
    )
    assert reason is not None
    assert "耗时" in reason


def test_meltdown_token_budget():
    """Token 消耗超过 max_total_tokens 时触发 token 熔断。"""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from Module.workflow_engine.core.sub_agent.replan import check_meltdown

    mock_ledger = MagicMock()
    mock_ledger.total_tokens = 150_000

    # 使用当前时间避免触发时间熔断
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reason = check_meltdown(
        plan_version=1, max_failure_count=5,
        max_task_duration_s=600.0, created_at=now_str,
        token_ledger=mock_ledger, max_total_tokens=100_000,
    )
    assert reason is not None
    assert "150000" in reason


def test_meltdown_no_token_ledger_skips_token_check():
    """token_ledger=None 时跳过 token 检查。"""
    from datetime import datetime, timezone
    from Module.workflow_engine.core.sub_agent.replan import check_meltdown

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reason = check_meltdown(
        plan_version=1, max_failure_count=5,
        max_task_duration_s=600.0, created_at=now_str,
        token_ledger=None, max_total_tokens=100_000,
    )
    assert reason is None


# ── _extract_node_results ───────────────────────────────────────────────────


def test_extract_node_results():
    """从 WorkflowCheckpoint 提取 node_states（NodeState 对象含 evaluation 字段）。"""
    agent = _make_sub_agent()
    cp = WorkflowCheckpoint(
        task=_task_state(),
        exec=ExecutionState(
            node_states={
                NodeId("a"): NodeState(
                    node_id=NodeId("a"),
                    summary="搜索完成",
                    error=None,
                ),
                NodeId("b"): NodeState(
                    node_id=NodeId("b"),
                    summary="",
                    error="工具超时",
                ),
            },
        ),
    )

    results = agent._extract_node_results(cp)

    assert isinstance(results, dict)
    assert results[NodeId("a")].summary == "搜索完成"
    assert results[NodeId("a")].error is None
    assert results[NodeId("b")].summary == ""
    assert results[NodeId("b")].error == "工具超时"


# ── _abort_error_code ───────────────────────────────────────────────────────


def test_abort_error_code_max_nodes():
    """MAX_NODES_EXCEEDED → PLAN_TOO_LARGE。"""
    agent = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.MAX_NODES_EXCEEDED,
            message="节点数超限",
        ),
    ]
    assert agent._abort_error_code(issues) == WorkflowErrorCode.PLAN_TOO_LARGE.value


def test_abort_error_code_max_depth():
    """MAX_DEPTH_EXCEEDED → PLAN_TOO_LARGE。"""
    agent = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.MAX_DEPTH_EXCEEDED,
            message="深度超限",
        ),
    ]
    assert agent._abort_error_code(issues) == WorkflowErrorCode.PLAN_TOO_LARGE.value


def test_abort_error_code_other():
    """非结构问题（MISSING_CONTRACT）→ PLANNING_FAILED。

    Batch 1.7（Challenger C3）：兜底值原为 `INTERNAL_ERROR`，语义错位——
    `INTERNAL_ERROR` 的语义是"引擎自己出故障"，而"required 节点缺 output_contract"
    是**规划器产出不合格**。报 internal_error 会让规划失败在用户/调试面上冒充成
    执行期内部错误，违反 T4（归因必须匹配真实失败阶段）。
    """
    agent = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.MISSING_CONTRACT,
            node_id=NodeId("a"),
            message="缺少 output_contract",
        ),
    ]
    assert agent._abort_error_code(issues) == WorkflowErrorCode.PLANNING_FAILED.value


def test_abort_error_code_cycle():
    """CYCLE → PLAN_TOO_LARGE（结构不合法）。"""
    agent = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.CYCLE,
            message="DAG 有环: A → B → A",
        ),
    ]
    assert agent._abort_error_code(issues) == WorkflowErrorCode.PLAN_TOO_LARGE.value


def test_abort_error_code_duplicate_id():
    """DUPLICATE_ID → PLAN_TOO_LARGE（结构不合法）。"""
    agent = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.DUPLICATE_ID,
            node_id=NodeId("a"),
            message="node_id 重复: a",
        ),
    ]
    assert agent._abort_error_code(issues) == WorkflowErrorCode.PLAN_TOO_LARGE.value


def test_abort_error_code_missing_dependency():
    """MISSING_DEPENDENCY → PLAN_TOO_LARGE（结构不合法）。"""
    agent = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.MISSING_DEPENDENCY,
            node_id=NodeId("b"),
            message="依赖的节点 x 不存在",
        ),
    ]
    assert agent._abort_error_code(issues) == WorkflowErrorCode.PLAN_TOO_LARGE.value


# ── 辅助函数 ────────────────────────────────────────────────────────────────


def _collect_mailbox_events(mailbox: WorkflowMailbox) -> list:
    """从 mailbox 中收集所有已推送事件（非阻塞）。"""
    events: list = []
    while True:
        ev = mailbox.get_nowait()
        if ev is None:
            break
        events.append(ev)
    return events


# ═══════════════════════════════════════════════════════════════════════════
# resume_from_pause()（从暂停恢复）
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resume_from_pause_not_paused_raises():
    """非 PAUSED 状态调 resume_from_pause → RuntimeError。"""
    from Module.workflow_engine.mailbox import WorkflowMailbox

    infra = AgentInfra(
        llm=MagicMock(),
        milvus=None,
        http=None,
        tool_registry=MagicMock(),
        constraints=ConstraintConfig(),
    )
    sub_agent = SubAgent(infra=infra, hitl_notifier=MagicMock())

    # 手动设 task_state 为非 PAUSED
    sub_agent._task_state = WorkflowState(
        task_id=WorkflowId("t1"),
        status=WorkflowStatus.RUNNING,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    sub_agent._mailbox = WorkflowMailbox(WorkflowId("t1"))

    with pytest.raises(RuntimeError, match="非 PAUSED"):
        await sub_agent.resume_from_pause()


@pytest.mark.asyncio
async def test_resume_from_pause_no_checkpoint_raises():
    """PAUSED 但 checkpoint 缺失 → RuntimeError + 状态切到 FAILED。"""
    from Module.workflow_engine.mailbox import WorkflowMailbox

    infra = AgentInfra(
        llm=MagicMock(),
        milvus=None,
        http=None,
        tool_registry=MagicMock(),
        constraints=ConstraintConfig(),
    )
    sub_agent = SubAgent(infra=infra, hitl_notifier=MagicMock())

    task_state = WorkflowState(
        task_id=WorkflowId("t1"),
        status=WorkflowStatus.PAUSED,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    sub_agent._task_state = task_state
    sub_agent._mailbox = WorkflowMailbox(WorkflowId("t1"))

    # 注入 mock CheckpointManager——load 返回 None（模拟缺失）
    mock_cm = MagicMock()
    mock_cm.load = AsyncMock(return_value=None)
    sub_agent._checkpoint_mgr = mock_cm

    with pytest.raises(RuntimeError, match="checkpoint 缺失"):
        await sub_agent.resume_from_pause()

    # 验证状态切到了 FAILED
    assert task_state.status == WorkflowStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════
# E3 Task 4: 终态不可逆——planning 写 RUNNING 前 _check_cancelled 拦截
# ═══════════════════════════════════════════════════════════════════════════


class TestTerminalMonotonic:
    @pytest.mark.asyncio
    async def test_planning_guard_blocks_when_cancelled(self):
        """真正驱动 planning 的 RUNNING 写点：planner.plan 执行期间 cancel 落地
        （ts.status=CANCELLED），plan_and_validate 到写 RUNNING 处 _check_cancelled
        抛 CancelledError、status 不被覆写。"""
        from Module.workflow_engine.core.sub_agent.planning import plan_and_validate
        ts = WorkflowState(task_id=WorkflowId("t1"), status=WorkflowStatus.PENDING,
                           tools=[], session_id=SessionId("s1"))
        planner = MagicMock()

        async def fake_plan(*a, **kw):
            ts.status = WorkflowStatus.CANCELLED      # 规划期间 cancel 落地
            return MagicMock()                         # 返回 mock plan（validator 放行）
        planner.plan = fake_plan
        validator = MagicMock()
        # validate 返回 (ValidationResult, PlanGraph)——ValidationResult 须有 .valid 属性
        validator.validate = MagicMock(return_value=(MagicMock(valid=True), MagicMock()))
        with pytest.raises(asyncio.CancelledError):
            await plan_and_validate(
                planner, validator, MagicMock(), ts, WorkflowMailbox(WorkflowId("t1")),
                ConstraintConfig(),
                push_mailbox_fn=AsyncMock(), abort_error_code_fn=lambda: "ABORT",
            )
        assert ts.status == WorkflowStatus.CANCELLED   # 未被覆写为 RUNNING
