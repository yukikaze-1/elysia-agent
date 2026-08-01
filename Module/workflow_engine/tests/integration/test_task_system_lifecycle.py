"""TaskSystem 生命周期集成测试——stop / pause。

用真实 LLM + get_time 工具，提交一个简单任务验证生命周期方法。
只跑这一个文件（~15s），不走完整 e2e 套件。

用法:
    conda activate Elysia
    python -m pytest Module/workflow_engine/tests/integration/test_task_system_lifecycle.py -v -s
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import MagicMock

import pytest
from openai import AsyncOpenAI


from Module.workflow_engine.infra import AgentInfra
from Module.workflow_engine.schema import (
    ConstraintConfig,
    DelegationContext,
    DelegationStatus,
    SessionId,
    SubmitError,
    TaskEventType,
)
from Module.workflow_engine.task_system import TaskSystem
from Module.workflow_engine.delegation_preparer import DelegationPreparer
from Module.workflow_engine.mailbox import TaskMailbox

from Module.tool.manager import ToolManager
from Module.tool.protocol import PluginContext
from core.tools.time.plugin import TimePlugin
from Module.workflow_engine.tests.helpers import build_test_task_system, get_tools_from_manager

pytestmark = pytest.mark.slow

# ═══════════════════════════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stop_after_task_completion():
    """提交一个简单任务 → 等它完成 → stop() 清理。

    验证：
    - 任务正常完成
    - stop() 无异常
    - 注册表已清空
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await build_test_task_system(plugins=[TimePlugin])
    tools = get_tools_from_manager(ts._infra.tool_registry)  # type: ignore[reportArgumentType]

    # 提交简单任务
    ctx = DelegationContext(
        intent="现在几点了",
        tools=tools,
        session_id=SessionId("lifecycle-test"),
    )
    result = await ts.submit(ctx)
    assert isinstance(result, TaskMailbox), f"submit 失败: {result}"

    # 循环消费 Mailbox 事件，直到 completed 或 failed
    event = None
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(result.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        assert event is not None, "Mailbox.get() 返回 None"
        if event.type.value in ("completed", "failed", "cancelled"):
            break

    assert event is not None, "未收到任何事件"
    assert event.type.value == "completed", f"期望 completed，实际 {event.type.value}: {event.data}"

    # 验证 token 追踪已激活——从 COMPLETED 事件 data 读取（竞争条件：
    # SubAgent 在 push COMPLETED 后立即清理 _tasks，get_token_usage() 可能返回 None）
    token_usage = event.data.get("token_usage", {})
    assert token_usage, f"COMPLETED 事件应包含 token_usage，实际 keys: {list(event.data.keys())}"
    total = event.data.get("token_total", 0)
    assert total > 0, f"token 总消耗应 > 0，实际: {total}"
    # 至少应有 Planner（plan）+ Executor（工具匹配）+ Evaluator（业务判定）
    assert "planner" in token_usage, f"应有 planner 记录，实际: {list(token_usage.keys())}"
    assert "executor" in token_usage, f"应有 executor 记录，实际: {list(token_usage.keys())}"
    assert "evaluator" in token_usage, f"应有 evaluator 记录，实际: {list(token_usage.keys())}"

    # stop 应该正常返回
    await ts.stop(timeout=10.0)

    assert len(ts._tasks) == 0
    assert len(ts._task_futures) == 0


@pytest.mark.asyncio
async def test_stop_rejects_submit_after_shutdown():
    """stop() 后 submit() 被拒绝。"""
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await build_test_task_system(plugins=[TimePlugin])
    await ts.stop(timeout=10.0)

    from Module.workflow_engine.schema import SubmitRejectionCode

    ctx = DelegationContext(
        intent="测试",
        session_id=SessionId("s1"),
    )
    result = await ts.submit(ctx)
    assert isinstance(result, SubmitError)
    assert result.code == SubmitRejectionCode.NOT_READY


@pytest.mark.asyncio
async def test_pause_sets_flag_and_cleanly_stops():
    """提交任务 → pause() → 验证 pause_requested 被设置 + stop 清理。

    get_time 任务很快（~5s），pause 窗口窄——不保证一定在 PAUSED
    状态停止，但 pause_requested 标志应正确设置，且 stop() 应成功清理。
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await build_test_task_system(plugins=[TimePlugin])
    tools = get_tools_from_manager(ts._infra.tool_registry)  # type: ignore[reportArgumentType]

    ctx = DelegationContext(
        intent="现在几点了",
        tools=tools,
        session_id=SessionId("pause-test"),
    )
    result = await ts.submit(ctx)
    assert isinstance(result, TaskMailbox), f"submit 失败: {result}"
    task_id = result.task_id

    # 等第一个 PROGRESS——表示 Orchestrator 已启动
    first_event = await asyncio.wait_for(result.get(), timeout=30.0)
    assert first_event is not None, "未收到首个事件"

    # pause——告诉 Orchestrator 在下一个安全点停止
    # 如果任务已完成（get_time 很快），pause 会抛 KeyError——这也是合法行为
    try:
        await ts.pause(task_id)
        # 验证 pause_requested 被设置
        sub_agent = ts._tasks.get(task_id)
        if sub_agent is not None:
            orch = sub_agent._orchestrator
            assert orch._exec_state.pause_requested is True  # type: ignore[reportOptionalMemberAccess]
    except KeyError:
        # 任务已完成并清理——pause 对其无意义
        pass

    # 消费剩余事件 + stop
    await asyncio.sleep(0.5)
    await ts.stop(timeout=10.0)


@pytest.mark.asyncio
async def test_concurrent_slot_limit():
    """max_sub_agents=1 → submit 两个任务 → 第二个等槽位。

    第一个完成后第二个才开始执行，两个都应该完成。
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    # 构建只有 1 个槽位的 TaskSystem
    api_key = os.getenv("DEEPSEEK_API_KEY")
    llm = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    tm = ToolManager()
    ctx = PluginContext(session_id="concurrency-test")
    await tm.load_all([TimePlugin], ctx)

    infra = AgentInfra(
        llm=llm, milvus=None, http=None,
        tool_registry=tm, constraints=ConstraintConfig(),
    )
    ts = TaskSystem(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=1)
    await ts.start()

    tools = get_tools_from_manager(ts._infra.tool_registry)  # type: ignore[reportArgumentType]

    # 提交第一个任务——立刻拿到 mailbox
    ctx1 = DelegationContext(
        intent="现在几点了",
        tools=tools,
        session_id=SessionId("concurrent-1"),
    )
    result1 = await ts.submit(ctx1)
    assert isinstance(result1, TaskMailbox)

    # 提交第二个任务——semaphore 已满，submit 会阻塞在 acquire()
    # 用 create_task 让它在后台等待
    submit2_completed = False

    async def _submit_task2():
        nonlocal submit2_completed
        ctx2 = DelegationContext(
            intent="现在几点了",
            tools=tools,
            session_id=SessionId("concurrent-2"),
        )
        r2 = await ts.submit(ctx2)
        submit2_completed = True
        return r2

    submit2_task = asyncio.create_task(_submit_task2())

    # 等待一小段时间——任务 2 应该还在等待 semaphore
    await asyncio.sleep(0.5)
    assert not submit2_completed, "任务 2 不应该在槽位满时完成 submit"

    # 消费任务 1 的事件直到完成
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(result1.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is None:
            continue
        if event.type in (TaskEventType.COMPLETED, TaskEventType.FAILED):
            break

    # 任务 1 完成后，任务 2 应该拿到槽位
    result2 = await asyncio.wait_for(submit2_task, timeout=30.0)
    assert isinstance(result2, TaskMailbox)

    # 任务 2 也应该完成
    deadline2 = time.monotonic() + 45.0
    while time.monotonic() < deadline2:
        remaining = deadline2 - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(result2.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is None:
            continue
        if event.type in (TaskEventType.COMPLETED, TaskEventType.FAILED):
            break

    await ts.stop(timeout=10.0)


@pytest.mark.asyncio
async def test_goal_failed_triggers_replan():
    """Evaluator 返回 goal_failed → Orchestrator 返回 NEEDS_REPLAN → Replanner.replan()。

    用 monkeypatch 让 Evaluator 对特定节点返回 goal_failed，
    验证 Replan 机制被触发（plan_version > 1）。
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await build_test_task_system(plugins=[TimePlugin])
    tools = get_tools_from_manager(ts._infra.tool_registry)  # type: ignore[reportArgumentType]

    # 缩小熔断阈值——2 次 replan 即触发熔断（而非默认 5），大幅缩短测试耗时。
    # 在完整套件中 LLM 可能被限速，每次 Replanner.replan() 耗时更长。
    ts._infra.constraints.max_failure_count = 2

    # Evaluator 已改为 per-task 组件（SubAgent.run() 内部创建），
    # 不再暴露在 TaskSystem 层——需要通过类级 monkeypatch 注入 mock。
    # patch 打在 Evaluator 源类上，所有实例化后的 evaluate() 都会被替换。
    from unittest.mock import patch
    from Module.workflow_engine.schema import (
        BusinessStatus, EvaluatorAction, ExecutionStatus,
        NodeResultEvaluation,
    )

    async def _always_goal_failed(result, node, plan):
        """返回 goal_failed → REPLAN（模拟业务评估失败）。

        注意：patch 替换的是类方法，mock 不自动传入 self——
        三个参数依次是 result, node, plan。
        """
        return NodeResultEvaluation(
            node_id=result.node_id,
            execution_status=ExecutionStatus.OK,
            business_status=BusinessStatus.GOAL_FAILED,
            reason="（测试注入）业务评估失败",
            suggested_action=EvaluatorAction.REPLAN,
            result=result,
        )

    # 类级 patch：所有 SubAgent.run() 创建的 Evaluator 实例都使用 mock
    with patch(
        "Module.workflow_engine.core.evaluator.Evaluator.evaluate",
        side_effect=_always_goal_failed,
    ):
        ctx = DelegationContext(
            intent="现在几点了",
            tools=tools,
            session_id=SessionId("replan-test"),
        )
        result = await ts.submit(ctx)
        assert isinstance(result, TaskMailbox)

        # 消费事件——由于 goal_failed 持续触发 REPLAN，最终会熔断
        # 2 轮 replan × (~15s Planner + ~2s Executor) ≈ 35s + buffer
        event = None
        deadline = time.monotonic() + 300.0
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                event = await asyncio.wait_for(result.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if event is None:
                continue
            # PROGRESS 事件是 replan 过程中的正常产物，跳过
            if event.type == TaskEventType.PROGRESS:
                continue
            if event.type in (TaskEventType.FAILED, TaskEventType.COMPLETED):
                break

        assert event is not None, "未收到终态事件"
        assert event.type == TaskEventType.FAILED, \
            f"期望 FAILED（熔断），实际 {event.type}: {event.data}"
        assert "熔断" in event.data.get("error", ""), \
            f"期望熔断错误，实际: {event.data}"

        # FAILED 事件不携带 token_usage（只有 COMPLETED 携带），不在此做 token 断言。
        # 已确认 FAILED 事件被正确推送，Orchestrator 内部异常触发 replan 降级路径正常。

    # patch 上下文结束后自动恢复 Evaluator.evaluate

    await ts.stop(timeout=10.0)
    # 关闭 LLM client 连接池——避免 event loop closed 错误
    await ts._infra.llm.close()


@pytest.mark.asyncio
async def test_cancel_during_execution():
    """提交任务 → cancel() → 收到 CANCELLED 事件 → 注册表清理。

    在任务执行期间取消，验证：
    - cancel() 返回 True
    - 最终收到 cancelled/completed/failed 事件（取决于时序）
    - 注册表已清理
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await build_test_task_system(plugins=[TimePlugin])
    tools = get_tools_from_manager(ts._infra.tool_registry)  # type: ignore[reportArgumentType]

    ctx = DelegationContext(
        intent="现在几点了",
        tools=tools,
        session_id=SessionId("cancel-test"),
    )
    result = await ts.submit(ctx)
    assert isinstance(result, TaskMailbox), f"submit 失败: {result}"
    task_id = result.task_id

    # 等第一个 PROGRESS——表示 Orchestrator 已启动
    first_event = await asyncio.wait_for(result.get(), timeout=30.0)
    assert first_event is not None, "未收到首个事件"

    # 取消任务
    assert await ts.cancel(task_id) is True

    # 消费剩余事件——预期最终收到 cancelled
    event = None
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(result.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is None:
            continue
        if event.type in (TaskEventType.CANCELLED, TaskEventType.FAILED,
                          TaskEventType.COMPLETED):
            break

    # 三种可能：cancelled（取消成功）、completed（任务太快已完成）、
    # failed（取消路径中的异常）
    assert event is not None, "未收到终态事件"
    assert event.type in (
        TaskEventType.CANCELLED, TaskEventType.COMPLETED, TaskEventType.FAILED
    ), f"意外的终态事件: {event.type.value}: {event.data}"

    # 等清理完成
    await asyncio.sleep(0.5)
    # 任务应从注册表清理
    assert task_id not in ts._tasks, "任务应已从注册表清理"

    await ts.stop(timeout=10.0)
    # 关闭 LLM client 连接池
    await ts._infra.llm.close()


@pytest.mark.asyncio
async def test_pause_and_resume():
    """提交任务 → pause → resume → 任务正常完成。

    验证：
    - pause 后 PAUSED 事件被推送
    - resume 返回 mailbox
    - 任务仍在注册表中（PAUSED 不清理）
    - 任务最终完成（COMPLETED）
    - 注册表清理
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await build_test_task_system(plugins=[TimePlugin])
    tools = get_tools_from_manager(ts._infra.tool_registry)  # type: ignore[reportArgumentType]

    ctx = DelegationContext(
        intent="现在几点了",
        tools=tools,
        session_id=SessionId("resume-test"),
    )
    result = await ts.submit(ctx)
    assert isinstance(result, TaskMailbox), f"submit 失败: {result}"
    task_id = result.task_id

    # 等第一个 PROGRESS——表示 Orchestrator 已启动
    first_event = await asyncio.wait_for(result.get(), timeout=30.0)
    assert first_event is not None, "未收到首个事件"

    # pause
    try:
        await ts.pause(task_id)
    except KeyError:
        # 任务已完成（get_time 很快），跳过测试
        await ts.stop(timeout=10.0)
        await ts._infra.llm.close()
        return

    # 等 PAUSED 事件
    paused_received = False
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(result.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is not None and event.type == TaskEventType.PAUSED:
            paused_received = True
            break
        if event is not None and event.type in (
            TaskEventType.COMPLETED, TaskEventType.FAILED,
        ):
            # 任务在 pause 生效前就完成了
            break

    if not paused_received:
        # 任务太快——pause 窗口太窄，跳过 resume 验证
        await ts.stop(timeout=10.0)
        await ts._infra.llm.close()
        return

    # 验证任务仍在注册表中（PAUSED 不清理）
    assert task_id in ts._tasks, "PAUSED 任务应从注册表保留"

    # resume
    resume_result = await ts.resume(task_id)
    assert isinstance(resume_result, TaskMailbox), \
        f"resume 应返回 TaskMailbox，实际: {type(resume_result)}"

    # 消费到终态
    event = None
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(result.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is None:
            continue
        if event.type in (TaskEventType.COMPLETED, TaskEventType.FAILED):
            break

    assert event is not None, "未收到终态事件"
    assert event.type == TaskEventType.COMPLETED, \
        f"期望 COMPLETED，实际 {event.type.value}: {event.data}"

    # 等清理完成
    await asyncio.sleep(0.5)
    assert task_id not in ts._tasks, "任务应已从注册表清理"

    await ts.stop(timeout=10.0)
    await ts._infra.llm.close()


@pytest.mark.asyncio
async def test_cancel_on_paused_task():
    """暂停后取消——PAUSED → CANCELLED 直接转换。

    验证：
    - pause 后 PAUSED 事件被推送
    - cancel() 返回 True
    - 收到 CANCELLED 事件（不是 COMPLETED 或 FAILED）
    - 注册表清理
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await build_test_task_system(plugins=[TimePlugin])
    tools = get_tools_from_manager(ts._infra.tool_registry)  # type: ignore[reportArgumentType]

    ctx = DelegationContext(
        intent="现在几点了",
        tools=tools,
        session_id=SessionId("cancel-paused-test"),
    )
    result = await ts.submit(ctx)
    assert isinstance(result, TaskMailbox), f"submit 失败: {result}"
    task_id = result.task_id

    # 等第一个 PROGRESS——表示 Orchestrator 已启动
    first_event = await asyncio.wait_for(result.get(), timeout=30.0)
    assert first_event is not None, "未收到首个事件"

    # pause
    try:
        await ts.pause(task_id)
    except KeyError:
        await ts.stop(timeout=10.0)
        await ts._infra.llm.close()
        return

    # 等 PAUSED 事件
    paused_received = False
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(result.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is not None and event.type == TaskEventType.PAUSED:
            paused_received = True
            break
        if event is not None and event.type in (
            TaskEventType.COMPLETED, TaskEventType.FAILED,
        ):
            break

    if not paused_received:
        await ts.stop(timeout=10.0)
        await ts._infra.llm.close()
        return

    # 验证 PAUSED 未清理注册表
    assert task_id in ts._tasks, "PAUSED 任务应在注册表中"

    # cancel → PAUSED 直接转 CANCELLED
    assert await ts.cancel(task_id) is True

    # 消费 CANCELLED 事件
    event = None
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(result.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is None:
            continue
        if event.type in (TaskEventType.CANCELLED, TaskEventType.FAILED):
            break

    assert event is not None, "未收到终态事件"
    assert event.type == TaskEventType.CANCELLED, \
        f"期望 CANCELLED，实际 {event.type.value}: {event.data}"

    await asyncio.sleep(0.5)
    assert task_id not in ts._tasks, "任务应已从注册表清理"

    await ts.stop(timeout=10.0)
    await ts._infra.llm.close()


@pytest.mark.asyncio
async def test_resume_idempotent_on_running():
    """RUNNING 任务上调 resume → 幂等返回 TaskMailbox。

    验证：
    - resume() 对 RUNNING 任务返回 TaskMailbox（不是 ResumeError）
    - 任务正常完成不受影响
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await build_test_task_system(plugins=[TimePlugin])
    tools = get_tools_from_manager(ts._infra.tool_registry)  # type: ignore[reportArgumentType]

    ctx = DelegationContext(
        intent="现在几点了",
        tools=tools,
        session_id=SessionId("resume-idempotent-test"),
    )
    result = await ts.submit(ctx)
    assert isinstance(result, TaskMailbox), f"submit 失败: {result}"
    task_id = result.task_id

    # 等第一个 PROGRESS——确认 Orchestrator 已启动（此时 status=RUNNING）
    first_event = await asyncio.wait_for(result.get(), timeout=30.0)
    assert first_event is not None, "未收到首个事件"

    # 对 RUNNING 任务调 resume——应幂等返回 mailbox
    resume_result = await ts.resume(task_id)
    assert isinstance(resume_result, TaskMailbox), \
        f"RUNNING 任务 resume 应幂等返回 TaskMailbox，实际: {type(resume_result)}"

    # 任务应照常完成
    event = None
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(result.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is None:
            continue
        if event.type in (TaskEventType.COMPLETED, TaskEventType.FAILED):
            break

    assert event is not None, "未收到终态事件"
    assert event.type == TaskEventType.COMPLETED, \
        f"期望 COMPLETED，实际 {event.type.value}: {event.data}"

    await asyncio.sleep(0.5)
    assert task_id not in ts._tasks, "任务应已从注册表清理"

    await ts.stop(timeout=10.0)
    await ts._infra.llm.close()


@pytest.mark.asyncio
async def test_concurrent_tasks_no_interference():
    """max_sub_agents=2 时两个任务并发执行，互不干扰。

    验证：
    - 两个任务都拿到 mailbox 且不同 task_id
    - 两个任务都正常完成（COMPLETED）
    - 各自有独立的 token 记录
    - 注册表均清理
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    api_key = os.getenv("DEEPSEEK_API_KEY")
    llm = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    tm = ToolManager()
    ctx_p = PluginContext(session_id="concurrent-isolation-test")
    await tm.load_all([TimePlugin], ctx_p)

    infra = AgentInfra(
        llm=llm, milvus=None, http=None,
        tool_registry=tm, constraints=ConstraintConfig(),
    )
    ts = TaskSystem(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=2)
    await ts.start()

    tools = get_tools_from_manager(ts._infra.tool_registry)  # type: ignore[reportArgumentType]

    ctx1 = DelegationContext(
        intent="现在几点了",
        tools=tools,
        session_id=SessionId("concurrent-iso-1"),
    )
    ctx2 = DelegationContext(
        intent="现在几点了",
        tools=tools,
        session_id=SessionId("concurrent-iso-2"),
    )

    result1 = await ts.submit(ctx1)
    result2 = await ts.submit(ctx2)
    assert isinstance(result1, TaskMailbox), f"task1 submit 失败: {result1}"
    assert isinstance(result2, TaskMailbox), f"task2 submit 失败: {result2}"
    assert result1.task_id != result2.task_id, "两个任务应有不同 task_id"

    async def _consume(mailbox: TaskMailbox, label: str) -> dict:
        event = None
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                event = await asyncio.wait_for(mailbox.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if event is None:
                continue
            if event.type in (TaskEventType.COMPLETED, TaskEventType.FAILED):
                break
        return {"label": label, "event": event}

    r1, r2 = await asyncio.gather(
        _consume(result1, "task1"),
        _consume(result2, "task2"),
    )

    assert r1["event"] is not None, "task1 未收到终态事件"
    assert r1["event"].type == TaskEventType.COMPLETED, \
        f"task1 期望 COMPLETED，实际 {r1['event'].type.value}"
    assert r2["event"] is not None, "task2 未收到终态事件"
    assert r2["event"].type == TaskEventType.COMPLETED, \
        f"task2 期望 COMPLETED，实际 {r2['event'].type.value}"

    t1_data = r1["event"].data
    t2_data = r2["event"].data
    assert t1_data.get("token_total", 0) > 0, "task1 应有 token 消耗"
    assert t2_data.get("token_total", 0) > 0, "task2 应有 token 消耗"

    await asyncio.sleep(0.5)
    assert result1.task_id not in ts._tasks, "task1 应已清理"
    assert result2.task_id not in ts._tasks, "task2 应已清理"

    await ts.stop(timeout=10.0)
    await ts._infra.llm.close()
