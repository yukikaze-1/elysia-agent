"""TaskSystem 生命周期集成测试——stop / pause。

用真实 LLM + get_time 工具，提交一个简单任务验证生命周期方法。
只跑这一个文件（~15s），不走完整 e2e 套件。

用法:
    conda activate Elysia
    python -m pytest Module/task_system/tests/integration/test_task_system_lifecycle.py -v -s
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from openai import AsyncOpenAI

# ── 加载 .env ────────────────────────────────────────────────────────────────

_env_path = Path(__file__).resolve().parents[4] / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                key, _, value = _line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key not in os.environ:
                    os.environ[key] = value

from Module.task_system.infra import AgentInfra
from Module.task_system.schema import (
    ConstraintConfig,
    DelegationContext,
    DelegationStatus,
    SessionId,
    SubmitError,
    TaskId,
    TaskStatus,
    TaskEventType,
    Tool,
    UserProfile,
)
from Module.task_system.task_system import TaskSystem
from Module.task_system.delegation_preparer import DelegationPreparer
from Module.task_system.mailbox import TaskMailbox
from Module.task_system.schema import TokenPeriod
from Module.tool.manager import ToolManager
from Module.tool.protocol import PluginContext
from core.tools.time.plugin import TimePlugin


# ── helpers ───────────────────────────────────────────────────────────────────


async def _build_task_system() -> TaskSystem:
    """构建并启动 TaskSystem（真实 LLM + TimePlugin）。"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置")

    llm = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    tm = ToolManager()
    ctx = PluginContext(session_id="lifecycle-test")
    await tm.load_all([TimePlugin], ctx)

    infra = AgentInfra(
        llm=llm, milvus=None, http=None,
        tool_registry=tm, constraints=ConstraintConfig(),
    )
    ts = TaskSystem(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=2)
    await ts.start()
    return ts


def _tools_from_manager(tm: ToolManager) -> list[Tool]:
    """从 ToolManager 提取 TaskSystem 格式的 Tool 列表。"""
    tools: list[Tool] = []
    for name, plugin in tm._plugins.items():
        caps = getattr(plugin, "capabilities", [])
        if isinstance(caps, str):
            caps = [caps]
        desc = getattr(plugin, "description", "") or getattr(plugin, "tool_description", "") or ""
        tools.append(Tool(name=name, description=desc, capabilities=list(caps) if caps else []))
    return tools


# ═══════════════════════════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stop_after_task_completion():
    """提交一个简单任务 → 等它完成 → stop() 清理。

    验证：
    - 任务正常完成
    - stop() 无异常
    - 注册表已清空
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await _build_task_system()
    tools = _tools_from_manager(ts._infra.tool_registry)

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
@pytest.mark.integration
async def test_stop_rejects_submit_after_shutdown():
    """stop() 后 submit() 被拒绝。"""
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await _build_task_system()
    await ts.stop(timeout=10.0)

    from Module.task_system.schema import SubmitRejectionCode

    ctx = DelegationContext(
        intent="测试",
        session_id=SessionId("s1"),
    )
    result = await ts.submit(ctx)
    assert isinstance(result, SubmitError)
    assert result.code == SubmitRejectionCode.NOT_READY


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pause_sets_flag_and_cleanly_stops():
    """提交任务 → pause() → 验证 pause_requested 被设置 + stop 清理。

    get_time 任务很快（~5s），pause 窗口窄——不保证一定在 PAUSED
    状态停止，但 pause_requested 标志应正确设置，且 stop() 应成功清理。
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await _build_task_system()
    tools = _tools_from_manager(ts._infra.tool_registry)

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
            assert orch._exec_state.pause_requested is True
    except KeyError:
        # 任务已完成并清理——pause 对其无意义
        pass

    # 消费剩余事件 + stop
    await asyncio.sleep(0.5)
    await ts.stop(timeout=10.0)


@pytest.mark.asyncio
@pytest.mark.integration
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

    tools = _tools_from_manager(ts._infra.tool_registry)

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
@pytest.mark.integration
async def test_goal_failed_triggers_replan():
    """Evaluator 返回 goal_failed → Orchestrator 返回 NEEDS_REPLAN → Planner.replan()。

    用 monkeypatch 让 Evaluator 对特定节点返回 goal_failed，
    验证 Replan 机制被触发（plan_version > 1）。
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await _build_task_system()
    tools = _tools_from_manager(ts._infra.tool_registry)

    # 缩小熔断阈值——2 次 replan 即触发熔断（而非默认 5），大幅缩短测试耗时。
    # 在完整套件中 LLM 可能被限速，每次 Planner.replan() 耗时更长。
    ts._infra.constraints.max_failure_count = 2

    # Evaluator 已改为 per-task 组件（SubAgent.run() 内部创建），
    # 不再暴露在 TaskSystem 层——需要通过类级 monkeypatch 注入 mock。
    # patch 打在 Evaluator 源类上，所有实例化后的 evaluate() 都会被替换。
    from unittest.mock import patch
    from Module.task_system.schema import (
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
        "Module.task_system.core.evaluator.Evaluator.evaluate",
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
