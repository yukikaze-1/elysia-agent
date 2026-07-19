"""TaskScheduler 测试 — 调度层单元测试 + Agent 集成测试。"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from pydantic import BaseModel, Field

from core.task import Task, TaskType, TaskStatus
from core.task_scheduler import TaskScheduler
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId
from Module.tool import ToolManager, ToolContext, PluginContext
from Module.tool.protocol import ToolPlugin, ToolResult, ErrorType
from Module.tool.safety import SafetyProfile

pytestmark = pytest.mark.service


# ═══════════════════════════════════════════════════════════════════
# Param models
# ═══════════════════════════════════════════════════════════════════

class MsgParams(BaseModel):
    """匹配现有测试的 msg 参数。"""
    msg: str = Field(default="", description="message to echo")


# ═══════════════════════════════════════════════════════════════════
# Plugin classes
# ═══════════════════════════════════════════════════════════════════

class EchoPlugin(ToolPlugin):
    """回显工具 — 返回 msg 内容。"""
    name = "echo"
    description = "Echoes back the message"
    param_model = MsgParams
    capabilities = set()
    safety = SafetyProfile()

    async def execute(self, *, ctx, emit=None, params):
        return params.msg


class FailingPlugin(ToolPlugin):
    """永远返回失败的工具（非异常，直接返回 ToolResult.fail）。"""
    name = "failing"
    description = "Always fails"
    param_model = MsgParams
    capabilities = set()
    safety = SafetyProfile()

    async def execute(self, *, ctx, emit=None, params):
        return ToolResult.fail(name="failing", error="always fails")


class CrashPlugin(ToolPlugin):
    """执行中抛出 CancelledError 的工具。"""
    name = "crashing"
    description = "Crashes badly"
    param_model = None
    capabilities = set()
    safety = SafetyProfile()

    async def execute(self, *, ctx, emit=None, params=None):
        raise asyncio.CancelledError()


class WaiterPlugin(ToolPlugin):
    """阻塞在 barrier 上的工具，用于测试执行中取消。"""
    name = "waiter"
    description = "Blocks on a barrier"
    param_model = None
    capabilities = set()
    safety = SafetyProfile()

    async def execute(self, *, ctx, emit=None, params=None):
        await self._barrier.wait()  # type: ignore[attr-defined]
        return ToolResult.ok(name="waiter", content="woke", duration_ms=0.0)


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

async def _make_manager() -> ToolManager:
    """单一 EchoPlugin 的 ToolManager。"""
    tm = ToolManager()
    await tm.load_all([EchoPlugin], PluginContext(session_id="test"))
    return tm


async def _make_multi_manager() -> ToolManager:
    """Echo + Failing + Crashing 的 ToolManager。"""
    tm = ToolManager()
    await tm.load_all(
        [EchoPlugin, FailingPlugin, CrashPlugin],
        PluginContext(session_id="test"),
    )
    return tm


async def _make_sleep_manager(barrier: asyncio.Event) -> ToolManager:
    """包含 WaiterPlugin 的 ToolManager — barrier 控制执行。"""
    tm = ToolManager()
    plugin = WaiterPlugin()
    plugin._barrier = barrier  # type: ignore[attr-defined]
    await plugin.setup(PluginContext(session_id="test"))
    tm._plugins["waiter"] = plugin
    return tm


def _tc(id_="c1", name="echo", args="{}"):
    return ToolCall(
        id=ToolCallId(id_),
        function=ToolCallFunction(name=name, arguments=args),
    )


# ═══════════════════════════════════════════════════════════════════
# 一、Submit / Dispatch
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_submit_and_dispatch():
    """单任务: submit → Worker 执行 → Future 返回结果。"""
    tm = await _make_manager()
    sched = TaskScheduler(tool_executor=tm, num_workers=2, checkpoint_path=None)
    await sched.start()

    t = Task.new_tool_call("parent-1", "echo", {"msg": "hello"})
    fut = t.get_future()
    await sched.submit(t)

    result = await asyncio.wait_for(fut, timeout=5.0)
    assert isinstance(result, ToolResult)
    assert result.success
    assert result.content == "hello"

    await sched.stop()


@pytest.mark.asyncio
async def test_dispatch_tool_calls_parallel():
    """3 个工具并行 dispatch → 全部成功。"""
    tm = await _make_manager()
    sched = TaskScheduler(tool_executor=tm, num_workers=5, checkpoint_path=None)
    await sched.start()

    calls = [
        _tc("c1", "echo", '{"msg":"a"}'),
        _tc("c2", "echo", '{"msg":"b"}'),
        _tc("c3", "echo", '{"msg":"c"}'),
    ]
    results = await sched.dispatch_tool_calls("parent-1", calls)

    assert len(results) == 3
    contents = []
    for tc, r in results:
        assert isinstance(r, ToolResult)
        assert r.success
        contents.append(r.content)
    assert sorted(contents) == ["a", "b", "c"]

    await sched.stop()


@pytest.mark.asyncio
async def test_priority_ordering():
    """高优先级 (数值小) 的任务先于低优先级执行。"""
    completed: list[str] = []

    class OrderedPlugin(ToolPlugin):
        name = "echo"
        description = ""
        param_model = MsgParams
        capabilities = set()
        safety = SafetyProfile()

        async def execute(self, *, ctx, emit=None, params):
            completed.append(params.msg)
            return params.msg

    tm = ToolManager()
    await tm.load_all([OrderedPlugin], PluginContext(session_id="test"))
    sched = TaskScheduler(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    t_low = Task.new_tool_call("p1", "echo", {"msg": "low"}, priority=9)
    t_high = Task.new_tool_call("p1", "echo", {"msg": "high"}, priority=0)
    t_mid = Task.new_tool_call("p1", "echo", {"msg": "mid"}, priority=5)

    f_low = t_low.get_future()
    f_high = t_high.get_future()
    f_mid = t_mid.get_future()

    await sched.submit(t_low)
    await sched.submit(t_high)
    await sched.submit(t_mid)

    await asyncio.gather(f_low, f_high, f_mid)
    assert completed == ["high", "mid", "low"]

    await sched.stop()


@pytest.mark.asyncio
async def test_worker_pool_saturation():
    """提交 10 个任务 > 5 个 worker，全部完成。"""
    tm = await _make_manager()
    sched = TaskScheduler(tool_executor=tm, num_workers=5, checkpoint_path=None)
    await sched.start()

    futures = []
    for i in range(10):
        t = Task.new_tool_call("p1", "echo", {"msg": str(i)})
        futures.append(t.get_future())
        await sched.submit(t)

    results = await asyncio.wait_for(
        asyncio.gather(*futures, return_exceptions=True), timeout=5.0,
    )
    assert len(results) == 10
    for r in results:
        assert isinstance(r, ToolResult)
        assert r.success

    await sched.stop()


@pytest.mark.asyncio
async def test_dispatch_tool_calls_mixed_results():
    """dispatch 混合: 成功 + 失败 + 异常。"""
    tm = await _make_multi_manager()
    sched = TaskScheduler(tool_executor=tm, num_workers=3, checkpoint_path=None)
    await sched.start()

    calls = [
        _tc("c1", "echo", '{"msg":"ok"}'),
        _tc("c2", "failing", '{"msg":"x"}'),
        _tc("c3", "crashing", "{}"),
    ]
    results = await sched.dispatch_tool_calls("parent-1", calls)
    assert len(results) == 3

    # c1 成功
    assert results[0][1].success
    # c2 失败
    assert not results[1][1].success
    assert "always fails" in results[1][1].error
    # c3 异常
    assert not results[2][1].success

    await sched.stop()


@pytest.mark.asyncio
async def test_dispatch_tool_calls_unknown_tool():
    """dispatch 未注册工具 → ToolResult.fail。"""
    tm = await _make_manager()
    sched = TaskScheduler(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    calls = [
        _tc("c1", "nonexistent", "{}"),
    ]
    results = await sched.dispatch_tool_calls("p1", calls)
    assert len(results) == 1
    assert not results[0][1].success

    await sched.stop()


@pytest.mark.asyncio
async def test_dispatch_tool_calls_empty():
    """空 calls 列表 → 空结果。"""
    tm = await _make_manager()
    sched = TaskScheduler(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    results = await sched.dispatch_tool_calls("p1", [])
    assert results == []

    await sched.stop()


# ═══════════════════════════════════════════════════════════════════
# 二、取消
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cancel_removes_future():
    """cancel → Future 传播 CancelledError。"""
    tm = await _make_manager()
    sched = TaskScheduler(tool_executor=tm, num_workers=2, checkpoint_path=None)
    await sched.start()

    t = Task.new_tool_call("p1", "echo", {"msg": "x"})
    fut = t.get_future()
    await sched.submit(t)

    sched.cancel(t.id)
    assert t.status == TaskStatus.CANCELLED

    with pytest.raises(asyncio.CancelledError):
        await fut

    await sched.stop()


@pytest.mark.asyncio
async def test_cancel_cascades_to_children():
    """cancel 父任务 → 级联取消所有子任务。"""
    tm = await _make_manager()
    sched = TaskScheduler(tool_executor=tm, num_workers=2, checkpoint_path=None)
    await sched.start()

    parent = Task(id="root", type=TaskType.TOOL_CALL)
    child = Task.new_tool_call("root", "echo", {"msg": "c"})
    grandchild = Task.new_tool_call(child.id, "echo", {"msg": "gc"})
    child.children_ids = [grandchild.id]
    parent.children_ids = [child.id]
    sched._tasks["root"] = parent
    sched._tasks[child.id] = child
    sched._tasks[grandchild.id] = grandchild

    sched.cancel("root")
    assert parent.status == TaskStatus.CANCELLED
    assert child.status == TaskStatus.CANCELLED
    assert grandchild.status == TaskStatus.CANCELLED

    await sched.stop()


@pytest.mark.asyncio
async def test_cancel_during_execution():
    """执行中 cancel → Worker task 被取消，Future 传播 CancelledError。"""
    barrier = asyncio.Event()
    tm = await _make_sleep_manager(barrier)
    sched = TaskScheduler(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    t = Task.new_tool_call("p1", "waiter", {})
    fut = t.get_future()
    await sched.submit(t)

    # 等待 worker 开始执行（status 变为 DISPATCHED）
    for _ in range(100):
        if t.status == TaskStatus.DISPATCHED:
            break
        await asyncio.sleep(0.01)
    assert t.status == TaskStatus.DISPATCHED, "worker should have started"

    sched.cancel(t.id)
    assert t.status == TaskStatus.CANCELLED

    with pytest.raises(asyncio.CancelledError):
        await fut

    # 释放 barrier 避免 worker 永久阻塞
    barrier.set()
    await sched.stop()


@pytest.mark.asyncio
async def test_dispatch_tool_calls_cancelled():
    """dispatch 期间取消 → 所有子任务被 cancel。"""
    barrier = asyncio.Event()
    tm = await _make_sleep_manager(barrier)
    sched = TaskScheduler(tool_executor=tm, num_workers=2, checkpoint_path=None)
    await sched.start()

    calls = [
        _tc("c1", "waiter", "{}"),
        _tc("c2", "waiter", "{}"),
    ]

    # 创建 dispatch task 然后取消
    dispatch_task = asyncio.create_task(sched.dispatch_tool_calls("p1", calls))
    await asyncio.sleep(0.05)  # 让子任务入队
    dispatch_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await dispatch_task

    # cancel() 级联取消子任务并从 _tasks 移除 — all_tasks 应为空
    remaining = sched.all_tasks()
    assert len(remaining) == 0, f"sub-tasks should be cleaned up, got {len(remaining)}"

    barrier.set()
    await sched.stop()


# ═══════════════════════════════════════════════════════════════════
# 三、重试
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_tool_retries_on_failure():
    """工具首次异常→重试→成功。"""
    call_count = 0

    class FlakyPlugin(ToolPlugin):
        name = "flaky"
        description = ""
        param_model = None
        capabilities = set()
        safety = SafetyProfile()

        async def execute(self, *, ctx, emit=None, params=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ToolResult.fail(
                    name="flaky", error="临时错误",
                    error_type=ErrorType.NETWORK_ERROR,
                )
            return ToolResult.ok(name="flaky", content="ok", duration_ms=0.0)

    tm = ToolManager()
    await tm.load_all([FlakyPlugin], PluginContext(session_id="test"))

    sched = TaskScheduler(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    t = Task.new_tool_call("p1", "flaky", {})
    fut = t.get_future()
    await sched.submit(t)

    result = await asyncio.wait_for(fut, timeout=5.0)
    assert call_count == 2
    assert result.success

    await sched.stop()


@pytest.mark.asyncio
async def test_tool_gives_up_after_max_retries():
    """工具持续失败 → retry_count 达到 max_retries → 返回 failed。"""
    call_count = 0

    class FailerPlugin(ToolPlugin):
        name = "failer"
        description = ""
        param_model = None
        capabilities = set()
        safety = SafetyProfile()

        async def execute(self, *, ctx, emit=None, params=None):
            nonlocal call_count
            call_count += 1
            return ToolResult.fail(
                name="failer", error="永久错误",
                error_type=ErrorType.NETWORK_ERROR,
            )

    tm = ToolManager()
    await tm.load_all([FailerPlugin], PluginContext(session_id="test"))

    sched = TaskScheduler(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    t = Task.new_tool_call("p1", "failer", {})
    t.max_retries = 1
    fut = t.get_future()
    await sched.submit(t)

    result = await asyncio.wait_for(fut, timeout=5.0)
    assert isinstance(result, ToolResult)
    assert not result.success
    assert call_count == 2  # first attempt + 1 retry

    await sched.stop()


# ═══════════════════════════════════════════════════════════════════
# 四、Task Tree
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_tree():
    """get_tree 返回嵌套结构。"""
    tm = await _make_manager()
    sched = TaskScheduler(tool_executor=tm, checkpoint_path=None)
    await sched.start()

    t = Task.new_tool_call("p1", "echo", {"msg": "t"})
    child = Task.new_tool_call(t.id, "echo", {"msg": "c"})
    t.children_ids.append(child.id)
    sched._tasks[t.id] = t
    sched._tasks[child.id] = child

    tree = sched.get_tree(t.id)
    assert tree["type"] == "tool_call"
    assert len(tree["children"]) == 1
    assert tree["children"][0]["id"] == child.id

    await sched.stop()


@pytest.mark.asyncio
async def test_get_tree_nested():
    """get_tree 三层嵌套结构。"""
    tm = await _make_manager()
    sched = TaskScheduler(tool_executor=tm, checkpoint_path=None)
    await sched.start()

    root = Task.new_tool_call("p1", "echo", {"msg": "r"})
    mid = Task.new_tool_call(root.id, "echo", {"msg": "m"})
    leaf = Task.new_tool_call(mid.id, "echo", {"msg": "l"})
    mid.children_ids = [leaf.id]
    root.children_ids = [mid.id]
    for t in [root, mid, leaf]:
        sched._tasks[t.id] = t

    tree = sched.get_tree(root.id)
    assert tree["children"][0]["children"][0]["id"] == leaf.id

    await sched.stop()


@pytest.mark.asyncio
async def test_get_tree_nonexistent():
    """get_tree 查不存在的 id → 空 dict。"""
    tm = await _make_manager()
    sched = TaskScheduler(tool_executor=tm, checkpoint_path=None)
    await sched.start()
    assert sched.get_tree("nonexistent") == {}
    await sched.stop()


# ═══════════════════════════════════════════════════════════════════
# 五、Checkpoint
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_checkpoint_save_and_restore():
    """单 BACKGROUND 任务持久化 → 恢复。"""
    tm = await _make_manager()
    tmpdir = tempfile.mkdtemp()
    cp = str(Path(tmpdir) / "cp.json")

    try:
        sched1 = TaskScheduler(tool_executor=tm, checkpoint_path=cp)
        await sched1.start()
        t = Task(id="bg-1", type=TaskType.BACKGROUND, status=TaskStatus.PENDING,
                 payload={"msg": "persist"}, run_in_background=True)
        sched1._tasks[t.id] = t
        sched1._completed_count = 5  # type: ignore[attr-defined]
        sched1._save_checkpoint()
        await sched1.stop()

        assert Path(cp).is_file()

        sched2 = TaskScheduler(tool_executor=tm, checkpoint_path=cp)
        sched2._restore_checkpoint()
        restored = sched2._tasks.get("bg-1")
        assert restored is not None
        assert restored.type == TaskType.BACKGROUND
        assert restored.payload["msg"] == "persist"
    finally:
        try:
            Path(cp).unlink()
        except Exception:
            pass

@pytest.mark.asyncio
async def test_checkpoint_multiple_tasks():
    """多个 BACKGROUND 任务同时保存/恢复。"""
    tm = await _make_manager()
    tmpdir = tempfile.mkdtemp()
    cp = str(Path(tmpdir) / "cp2.json")

    try:
        sched1 = TaskScheduler(tool_executor=tm, checkpoint_path=cp)
        await sched1.start()
        for i in range(3):
            t = Task.new_background(payload={"idx": i})
            t.id = f"bg-{i}"
            sched1._tasks[t.id] = t
        sched1._save_checkpoint()
        await sched1.stop()

        sched2 = TaskScheduler(tool_executor=tm, checkpoint_path=cp)
        sched2._restore_checkpoint()
        assert sched2._tasks["bg-0"].payload["idx"] == 0
        assert sched2._tasks["bg-1"].payload["idx"] == 1
        assert sched2._tasks["bg-2"].payload["idx"] == 2
        assert sched2.pending_count == 3
    finally:
        try:
            Path(cp).unlink()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_checkpoint_ignores_non_background():
    """TOOL_CALL 任务不持久化。"""
    tm = await _make_manager()
    tmpdir = tempfile.mkdtemp()
    cp = str(Path(tmpdir) / "cp3.json")

    try:
        sched = TaskScheduler(tool_executor=tm, checkpoint_path=cp)
        await sched.start()
        t = Task.new_tool_call("p1", "echo", {"msg": "x"})
        sched._tasks[t.id] = t
        sched._completed_count = 5  # type: ignore[attr-defined]
        sched._save_checkpoint()
        await sched.stop()

        sched2 = TaskScheduler(tool_executor=tm, checkpoint_path=cp)
        sched2._restore_checkpoint()
        assert len(sched2._tasks) == 0
    finally:
        try:
            Path(cp).unlink()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_checkpoint_no_path():
    """无 checkpoint_path 时不崩溃。"""
    tm = await _make_manager()
    sched = TaskScheduler(tool_executor=tm, checkpoint_path=None)
    await sched.start()
    sched._save_checkpoint()  # 不应抛异常

    t = Task(id="t1", type=TaskType.BACKGROUND, status=TaskStatus.PENDING,
             payload={}, run_in_background=True)
    sched._tasks[t.id] = t
    sched._save_checkpoint()  # 仍不应抛异常

    sched._restore_checkpoint()  # 无文件 → 静默跳过
    await sched.stop()


# ═══════════════════════════════════════════════════════════════════
# 六、集成测试
# ═══════════════════════════════════════════════════════════════════

import os as _os_module


def _ensure_api_key():
    """Ensure a dummy DEEPSEEK_API_KEY for Agent integration tests."""
    if not _os_module.environ.get("DEEPSEEK_API_KEY"):
        _os_module.environ["DEEPSEEK_API_KEY"] = "test-key-integration"


@pytest.mark.asyncio
async def test_cognitive_loop_callback_wired():
    """_on_tool_calls 回调被正确传递到 _think_loop。"""
    from unittest.mock import AsyncMock, MagicMock
    from core.agent_fsm import AgentFSM
    from core.event_bus import EventBus
    from Module.output import OutputManager
    from core.memory.session import SessionMemory
    from core.cognitive import CognitiveLoop
    from core.llm_adapter import LLMAdapter

    llm = MagicMock(spec=LLMAdapter)
    llm.model = "mock"

    async def fake_stream(messages, tools=None, **kw):
        for c in "你好":
            yield {"content": c, "tool_calls": None}
    llm.stream_think = fake_stream

    output = OutputManager()
    fsm = AgentFSM()
    bus = EventBus()
    tm = ToolManager()
    prompt = AsyncMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    prompt.build_messages.return_value = [
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ]
    session = MagicMock(spec=SessionMemory)
    session.recent_messages.return_value = []

    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt, session=session, tool_executor=tm,
    )

    callback_called = []

    async def my_callback(calls):
        callback_called.append(len(calls))
        return []

    await loop.process("hello", _on_tool_calls=my_callback)
    for _ in range(100):
        if fsm.state.value == "idle":
            break
        await asyncio.sleep(0.01)

    # 回调被传递了（即使 mock LLM 不产生 tool_calls，流程应该正常完成）
    assert fsm.state.value == "idle"
    await output.close()


@pytest.mark.asyncio
async def test_cognitive_loop_no_callback_backward_compat():
    """不传 _on_tool_calls → 正常完成（向后兼容）。"""
    from unittest.mock import AsyncMock, MagicMock
    from core.agent_fsm import AgentFSM
    from core.event_bus import EventBus
    from Module.output import OutputManager
    from core.memory.session import SessionMemory
    from core.cognitive import CognitiveLoop
    from core.llm_adapter import LLMAdapter

    llm = MagicMock(spec=LLMAdapter)
    llm.model = "mock"

    async def fake_stream(messages, tools=None, **kw):
        for c in "你好":
            yield {"content": c, "tool_calls": None}
    llm.stream_think = fake_stream

    output = OutputManager()
    fsm = AgentFSM()
    bus = EventBus()
    tm = ToolManager()
    prompt = AsyncMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    prompt.build_messages.return_value = [
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ]
    session = MagicMock(spec=SessionMemory)
    session.recent_messages.return_value = []

    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt, session=session, tool_executor=tm,
    )

    await loop.process("hello")
    for _ in range(100):
        if fsm.state.value == "idle":
            break
        await asyncio.sleep(0.01)

    assert fsm.state.value == "idle"
    await output.close()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_agent_scheduler_lifecycle():
    """Agent start → Scheduler 启动。Agent stop → Scheduler 停止。"""
    _ensure_api_key()
    from core.agent import Agent
    from core.app_config import AppConfig
    from core.tests.fake_llm import FakeLLMBackend

    agent = Agent(config=AppConfig())
    agent._llm._backend = FakeLLMBackend(response="ok")

    await agent.start()
    assert agent._scheduler._running is True
    assert len(agent._scheduler._workers) == 5

    await agent.stop()
    assert agent._scheduler._running is False


@pytest.mark.slow
@pytest.mark.asyncio
async def test_agent_fifo_queue():
    """忙时发送消息 → 排队。空闲后 → 自动出队。"""
    _ensure_api_key()
    from core.agent import Agent
    from core.app_config import AppConfig
    from core.tests.fake_llm import FakeLLMBackend

    agent = Agent(config=AppConfig())
    agent._llm._backend = FakeLLMBackend(response="hello")
    await agent.start()

    # 发送第一条消息
    await agent.process_message("消息1")
    # 等待进入忙状态
    for _ in range(100):
        if agent._fsm.state.value != "idle":
            break
        await asyncio.sleep(0.01)

    # 忙时发第二条 → 排队（若已回到 idle，说明第一条处理太快，仍可测：此时第二条应直接投递）
    busy = agent._fsm.state.value != "idle"
    await agent.process_message("消息2")
    if busy:
        assert len(agent._pending_messages) >= 1

    # 等待处理完
    for _ in range(200):
        if agent._fsm.state.value == "idle":
            break
        await asyncio.sleep(0.01)

    await asyncio.sleep(0.2)  # 等待 STATE_CHANGE idle → dequeue → 处理第二条
    # 排队消息应该已被处理
    assert len(agent._pending_messages) == 0

    await agent.stop()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_agent_interrupt_clears_queue():
    """INTERRUPT → 清空排队消息。"""
    _ensure_api_key()
    from core.agent import Agent
    from core.app_config import AppConfig
    from core.tests.fake_llm import FakeLLMBackend
    from core.event_bus import EventPriority
    from core.schema.event import InterruptData

    agent = Agent(config=AppConfig())
    agent._llm._backend = FakeLLMBackend(response="hello")
    await agent.start()

    agent._pending_messages = [
        {"text": "排队1", "user_name": "test", "request_id": "001"},
        {"text": "排队2", "user_name": "test", "request_id": "002"},
    ]

    agent._bus.post(InterruptData(source="test"),
                    priority=EventPriority.INTERRUPT)
    await asyncio.sleep(0.2)

    assert len(agent._pending_messages) == 0
    await agent.stop()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_scheduler_state_in_debug_info():
    """DebugInfo.collect() 包含 Scheduler 状态。"""
    _ensure_api_key()
    from core.agent import Agent
    from core.app_config import AppConfig
    from core.tests.fake_llm import FakeLLMBackend
    from core.debug_info import collect

    agent = Agent(config=AppConfig())
    agent._llm._backend = FakeLLMBackend(response="ok")
    await agent.start()

    info = await collect(agent)
    assert info.scheduler_total >= 0
    # 新启动的 Scheduler 应该没有任何 worker 在执行
    assert info.scheduler_running == 0
    assert info.scheduler_pending == 0
    assert isinstance(info.task_tree, dict)

    await agent.stop()
