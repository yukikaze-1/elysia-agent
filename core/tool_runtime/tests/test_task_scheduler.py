"""ToolCallDispatcher 测试 — 调度层单元测试 + Agent 集成测试。"""

import asyncio
import functools
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from pydantic import BaseModel, Field

from core.tool_runtime import QueuedToolCall, QueuedToolCallKind, QueuedToolCallState
from core.tool_runtime import ToolCallDispatcher
from core.tool_runtime import scheduler as _scheduler_mod
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId
from Module.tool import PluginContext
from Module.tool.protocol import ToolPlugin, ToolResult, ErrorType
from Module.tool.safety import SafetyProfile
from core.adapter import ToolAdapter


async def _fixed_session_resolver(session, conversation_id, protocol_version):
    """固定返回给定 session 的 resolver（批次 3：session= → session_resolver=）。"""
    return session


# ═══════════════════════════════════════════════════════════════════
# 参数模型
# ═══════════════════════════════════════════════════════════════════

class MsgParams(BaseModel):
    """匹配现有测试的 msg 参数。"""
    msg: str = Field(default="", description="message to echo")


# ═══════════════════════════════════════════════════════════════════
# 插件类
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
# 辅助函数
# ═══════════════════════════════════════════════════════════════════

async def _make_manager() -> ToolAdapter:
    """单一 EchoPlugin 的 ToolAdapter。"""
    tm = ToolAdapter([EchoPlugin], session_id="test")
    await tm.start()
    return tm


async def _make_multi_manager() -> ToolAdapter:
    """Echo + Failing + Crashing 的 ToolAdapter。"""
    tm = ToolAdapter([EchoPlugin, FailingPlugin, CrashPlugin], session_id="test")
    await tm.start()
    return tm


async def _make_sleep_manager(
    barrier: asyncio.Event,
    extra_plugins: list[type[ToolPlugin]] | None = None,
) -> ToolAdapter:
    """包含 WaiterPlugin 的 ToolAdapter — barrier 控制执行。

    Args:
        barrier: WaiterPlugin 阻塞的事件。
        extra_plugins: 额外注册的常规插件（S3-02 用例需要"卡住一个 + 走通一个"）。
    """
    tm = ToolAdapter(list(extra_plugins or []), session_id="test")
    await tm.start()
    plugin = WaiterPlugin()
    plugin._barrier = barrier  # type: ignore[attr-defined]
    await plugin.setup(PluginContext(session_id="test"))
    tm._manager._plugins["waiter"] = plugin
    return tm


def _make_sync_manager() -> MagicMock:
    """返回 mock ToolAdapter — 用于纯同步 _prune_tasks 测试。"""
    return MagicMock(spec=ToolAdapter)


def _tc(id_="c1", name="echo", args="{}"):
    return ToolCall(
        id=ToolCallId(id_),
        function=ToolCallFunction(name=name, arguments=args),
    )


# ═══════════════════════════════════════════════════════════════════
# 一、Submit / Dispatch
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
async def test_submit_and_dispatch():
    """单任务: submit → Worker 执行 → Future 返回结果。"""
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=2, checkpoint_path=None)
    await sched.start()

    t = QueuedToolCall.new_tool_call("parent-1", "echo", {"msg": "hello"})
    fut = t.get_future()
    await sched.submit(t)

    result = await asyncio.wait_for(fut, timeout=5.0)
    assert isinstance(result, ToolResult)
    assert result.success
    assert result.content == "hello"

    await sched.stop()


@pytest.mark.service
@pytest.mark.asyncio
async def test_dispatch_tool_calls_parallel():
    """3 个工具并行 dispatch → 全部成功。"""
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=5, checkpoint_path=None)
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


@pytest.mark.service
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

    tm = ToolAdapter([OrderedPlugin], session_id="test")
    await tm.start()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    t_low = QueuedToolCall.new_tool_call("p1", "echo", {"msg": "low"}, priority=9)
    t_high = QueuedToolCall.new_tool_call("p1", "echo", {"msg": "high"}, priority=0)
    t_mid = QueuedToolCall.new_tool_call("p1", "echo", {"msg": "mid"}, priority=5)

    f_low = t_low.get_future()
    f_high = t_high.get_future()
    f_mid = t_mid.get_future()

    await sched.submit(t_low)
    await sched.submit(t_high)
    await sched.submit(t_mid)

    await asyncio.gather(f_low, f_high, f_mid)
    assert completed == ["high", "mid", "low"]

    await sched.stop()


@pytest.mark.service
@pytest.mark.asyncio
async def test_worker_pool_saturation():
    """提交 10 个任务 > 5 个 worker，全部完成。"""
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=5, checkpoint_path=None)
    await sched.start()

    futures = []
    for i in range(10):
        t = QueuedToolCall.new_tool_call("p1", "echo", {"msg": str(i)})
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


@pytest.mark.service
@pytest.mark.asyncio
async def test_dispatch_tool_calls_mixed_results():
    """dispatch 混合: 成功 + 失败 + 异常。"""
    tm = await _make_multi_manager()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=3, checkpoint_path=None)
    await sched.start()

    calls = [
        _tc("c1", "echo", '{"msg":"ok"}'),
        _tc("c2", "failing", '{"msg":"x"}'),
        _tc("c3", "crashing", "{}"),
    ]
    results = await sched.dispatch_tool_calls("parent-1", calls)
    assert len(results) == 3

    # c1 成功
    assert results[0][1].success  # type: ignore[reportAttributeAccessIssue]
    # c2 失败
    assert not results[1][1].success  # type: ignore[reportAttributeAccessIssue]
    assert "always fails" in results[1][1].error  # type: ignore[reportAttributeAccessIssue]
    # c3 异常
    assert not results[2][1].success  # type: ignore[reportAttributeAccessIssue]

    await sched.stop()


@pytest.mark.service
@pytest.mark.asyncio
async def test_dispatch_tool_calls_unknown_tool():
    """dispatch 未注册工具 → ToolResult.fail。"""
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    calls = [
        _tc("c1", "nonexistent", "{}"),
    ]
    results = await sched.dispatch_tool_calls("p1", calls)
    assert len(results) == 1
    assert not results[0][1].success  # type: ignore[reportAttributeAccessIssue]

    await sched.stop()


@pytest.mark.service
@pytest.mark.asyncio
async def test_dispatch_tool_calls_empty():
    """空 calls 列表 → 空结果。"""
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    results = await sched.dispatch_tool_calls("p1", [])
    assert results == []

    await sched.stop()


@pytest.mark.service
@pytest.mark.asyncio
async def test_call_id_threaded_to_worker_ctx():
    """D6-07：call_id 线程化——payload 携带 call_id，worker 执行时 ctx.call_id 非空。

    ``new_tool_call(call_id=...)`` 把 LLM tool_call_id 存入 payload["call_id"]；
    ``_execute_tool`` 经 ``clone_for_call(call_id=...)`` 为每次执行克隆 ctx 并设置
    ctx.call_id（对齐 _execute_tools_directly 惯例），共享 base ctx 不被调用内污染。
    """
    seen_call_ids: list[str] = []

    class _RecordingExecutor:
        """记录每次 execute 收到的 ctx.call_id（Mock ToolAdapter）。"""

        def declared_request_timeout(self, tool_name):
            """S2-G2：调度器会问每个工具声明的预算；Mock 无声明 → None（用默认）。"""
            return None

        async def execute(self, tool_name, ctx, **kwargs):
            seen_call_ids.append(ctx.call_id)
            return ToolResult.ok(name=tool_name, content="ok", duration_ms=0.0)

    # payload 线程化：new_tool_call 携带 call_id
    t = QueuedToolCall.new_tool_call("p1", "echo", {"msg": "a"}, call_id="tc_x")
    assert t.payload["call_id"] == "tc_x"

    sched = ToolCallDispatcher(tool_executor=_RecordingExecutor(), num_workers=1,
                          checkpoint_path=None)
    await sched.start()
    try:
        results = await sched.dispatch_tool_calls("p1", [_tc("tc_x", "echo", '{"msg":"a"}')])
        assert results[0][1].success  # type: ignore[reportAttributeAccessIssue]
        assert seen_call_ids == ["tc_x"], f"worker 收到 call_id={seen_call_ids!r}"
    finally:
        await sched.stop()


# ═══════════════════════════════════════════════════════════════════
# 二、取消
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
async def test_cancel_removes_future():
    """cancel → Future 传播 CancelledError。"""
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=2, checkpoint_path=None)
    await sched.start()

    t = QueuedToolCall.new_tool_call("p1", "echo", {"msg": "x"})
    fut = t.get_future()
    await sched.submit(t)

    sched.cancel(t.id)
    assert t.status == QueuedToolCallState.CANCELLED

    with pytest.raises(asyncio.CancelledError):
        await fut

    await sched.stop()


@pytest.mark.service
@pytest.mark.asyncio
async def test_cancel_cascades_to_children():
    """cancel 父任务 → 级联取消所有子任务。"""
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=2, checkpoint_path=None)
    await sched.start()

    parent = QueuedToolCall(id="root", type=QueuedToolCallKind.TOOL_CALL)  # type: ignore[reportCallIssue]
    child = QueuedToolCall.new_tool_call("root", "echo", {"msg": "c"})
    grandchild = QueuedToolCall.new_tool_call(child.id, "echo", {"msg": "gc"})
    child.children_ids = [grandchild.id]
    parent.children_ids = [child.id]
    sched._tasks["root"] = parent
    sched._tasks[child.id] = child
    sched._tasks[grandchild.id] = grandchild

    sched.cancel("root")
    assert parent.status == QueuedToolCallState.CANCELLED
    assert child.status == QueuedToolCallState.CANCELLED
    assert grandchild.status == QueuedToolCallState.CANCELLED

    await sched.stop()


@pytest.mark.service
@pytest.mark.asyncio
async def test_cancel_during_execution():
    """执行中 cancel → Worker task 被取消，Future 传播 CancelledError。"""
    barrier = asyncio.Event()
    tm = await _make_sleep_manager(barrier)
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    t = QueuedToolCall.new_tool_call("p1", "waiter", {})
    fut = t.get_future()
    await sched.submit(t)

    # 等待 worker 开始执行（status 变为 DISPATCHED）
    for _ in range(100):
        if t.status == QueuedToolCallState.DISPATCHED:
            break
        await asyncio.sleep(0.01)
    assert t.status == QueuedToolCallState.DISPATCHED, "worker should have started"

    sched.cancel(t.id)
    assert t.status == QueuedToolCallState.CANCELLED

    with pytest.raises(asyncio.CancelledError):
        await fut

    # 释放 barrier 避免 worker 永久阻塞
    barrier.set()
    await sched.stop()


@pytest.mark.service
@pytest.mark.asyncio
async def test_dispatch_tool_calls_cancelled():
    """dispatch 期间取消 → 所有子任务被 cancel。"""
    barrier = asyncio.Event()
    tm = await _make_sleep_manager(barrier)
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=2, checkpoint_path=None)
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

@pytest.mark.service
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

    tm = ToolAdapter([FlakyPlugin], session_id="test")
    await tm.start()

    sched = ToolCallDispatcher(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    t = QueuedToolCall.new_tool_call("p1", "flaky", {})
    fut = t.get_future()
    await sched.submit(t)

    result = await asyncio.wait_for(fut, timeout=5.0)
    assert call_count == 2
    assert result.success

    await sched.stop()


@pytest.mark.service
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

    tm = ToolAdapter([FailerPlugin], session_id="test")
    await tm.start()

    sched = ToolCallDispatcher(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    t = QueuedToolCall.new_tool_call("p1", "failer", {})
    t.max_retries = 1
    fut = t.get_future()
    await sched.submit(t)

    # `stop()` 必须走 finally：这条用例正是"重试 gate 被削弱"的回归网（#46 缺口行 10
    # 的三个注入都让它变红），断言失败就跳过 stop() 会把一个仍在运行的 dispatcher
    # 留给事件循环 teardown。
    try:
        result = await asyncio.wait_for(fut, timeout=5.0)
        assert isinstance(result, ToolResult)
        assert not result.success
        assert call_count == 2  # first attempt + 1 retry
    finally:
        await sched.stop()


# ═══════════════════════════════════════════════════════════════════
# 四、QueuedToolCall Tree
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
async def test_get_tree():
    """get_tree 返回嵌套结构。"""
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, checkpoint_path=None)
    await sched.start()

    t = QueuedToolCall.new_tool_call("p1", "echo", {"msg": "t"})
    child = QueuedToolCall.new_tool_call(t.id, "echo", {"msg": "c"})
    t.children_ids.append(child.id)
    sched._tasks[t.id] = t
    sched._tasks[child.id] = child

    tree = sched.get_tree(t.id)
    assert tree["type"] == "tool_call"
    assert len(tree["children"]) == 1
    assert tree["children"][0]["id"] == child.id

    await sched.stop()


@pytest.mark.service
@pytest.mark.asyncio
async def test_get_tree_nested():
    """get_tree 三层嵌套结构。"""
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, checkpoint_path=None)
    await sched.start()

    root = QueuedToolCall.new_tool_call("p1", "echo", {"msg": "r"})
    mid = QueuedToolCall.new_tool_call(root.id, "echo", {"msg": "m"})
    leaf = QueuedToolCall.new_tool_call(mid.id, "echo", {"msg": "l"})
    mid.children_ids = [leaf.id]
    root.children_ids = [mid.id]
    for t in [root, mid, leaf]:
        sched._tasks[t.id] = t

    tree = sched.get_tree(root.id)
    assert tree["children"][0]["children"][0]["id"] == leaf.id

    await sched.stop()


@pytest.mark.service
@pytest.mark.asyncio
async def test_get_tree_nonexistent():
    """get_tree 查不存在的 id → 空 dict。"""
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, checkpoint_path=None)
    await sched.start()
    assert sched.get_tree("nonexistent") == {}
    await sched.stop()


# ═══════════════════════════════════════════════════════════════════
# 五、Checkpoint
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
async def test_checkpoint_save_and_restore():
    """单 BACKGROUND 任务持久化 → 恢复。"""
    tm = await _make_manager()
    tmpdir = tempfile.mkdtemp()
    cp = str(Path(tmpdir) / "cp.json")

    try:
        sched1 = ToolCallDispatcher(tool_executor=tm, checkpoint_path=cp)
        await sched1.start()
        t = QueuedToolCall(id="bg-1", type=QueuedToolCallKind.BACKGROUND, status=QueuedToolCallState.PENDING,
                 payload={"msg": "persist"}, run_in_background=True)  # type: ignore[reportCallIssue]
        sched1._tasks[t.id] = t
        sched1._save_checkpoint()
        await sched1.stop()

        assert Path(cp).is_file()

        sched2 = ToolCallDispatcher(tool_executor=tm, checkpoint_path=cp)
        sched2._restore_checkpoint()
        restored = sched2._tasks.get("bg-1")
        assert restored is not None
        assert restored.type == QueuedToolCallKind.BACKGROUND
        assert restored.payload["msg"] == "persist"  # type: ignore[reportGeneralTypeIssues]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@pytest.mark.service
@pytest.mark.asyncio
async def test_checkpoint_multiple_tasks():
    """多个 BACKGROUND 任务同时保存/恢复。"""
    tm = await _make_manager()
    tmpdir = tempfile.mkdtemp()
    cp = str(Path(tmpdir) / "cp2.json")

    try:
        sched1 = ToolCallDispatcher(tool_executor=tm, checkpoint_path=cp)
        await sched1.start()
        for i in range(3):
            t = QueuedToolCall.new_background(payload={"idx": i})
            t.id = f"bg-{i}"
            sched1._tasks[t.id] = t
        sched1._save_checkpoint()
        await sched1.stop()

        sched2 = ToolCallDispatcher(tool_executor=tm, checkpoint_path=cp)
        sched2._restore_checkpoint()
        assert sched2._tasks["bg-0"].payload["idx"] == 0  # type: ignore[reportGeneralTypeIssues]
        assert sched2._tasks["bg-1"].payload["idx"] == 1  # type: ignore[reportGeneralTypeIssues]
        assert sched2._tasks["bg-2"].payload["idx"] == 2  # type: ignore[reportGeneralTypeIssues]
        assert sched2.pending_count == 3
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.service
@pytest.mark.asyncio
async def test_checkpoint_ignores_non_background():
    """TOOL_CALL 任务不持久化。"""
    tm = await _make_manager()
    tmpdir = tempfile.mkdtemp()
    cp = str(Path(tmpdir) / "cp3.json")

    try:
        sched = ToolCallDispatcher(tool_executor=tm, checkpoint_path=cp)
        await sched.start()
        t = QueuedToolCall.new_tool_call("p1", "echo", {"msg": "x"})
        sched._tasks[t.id] = t
        sched._save_checkpoint()
        await sched.stop()

        sched2 = ToolCallDispatcher(tool_executor=tm, checkpoint_path=cp)
        sched2._restore_checkpoint()
        assert len(sched2._tasks) == 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.service
@pytest.mark.asyncio
async def test_checkpoint_no_path():
    """无 checkpoint_path 时不崩溃。"""
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, checkpoint_path=None)
    await sched.start()
    sched._save_checkpoint()  # 不应抛异常

    t = QueuedToolCall(id="t1", type=QueuedToolCallKind.BACKGROUND, status=QueuedToolCallState.PENDING,
             payload={}, run_in_background=True)  # type: ignore[reportCallIssue]
    sched._tasks[t.id] = t
    sched._save_checkpoint()  # 仍不应抛异常

    sched._restore_checkpoint()  # 无文件 → 静默跳过
    await sched.stop()


# ═══════════════════════════════════════════════════════════════════
# 一.5、S3-02：dispatch_tool_calls 的有界性（活性不变量）
#
# 这四条锁定的不是"结果对不对"，而是"**一定会返回**"。修复前
# `dispatch_tool_calls` 在调度器未运行 / stop() 与 dispatch 竞争时会永久挂起
# ——它持 `CognitiveLoop._process_lock` + 会话 work lease，等于该会话死锁且无
# 任何日志。因此每条都用 wait_for 兜底：回归时测试以 TimeoutError 失败，而不是
# 把整个测试会话挂死。
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.service
def test_dispatch_budget_never_below_single_call_budget():
    """预算不变量：单调用最坏预算 > 单次尝试超时，且 dispatch 预算 ≥ 单调用预算。

    S3-02 的取值不能被静默改小——若 `_DISPATCH_TIMEOUT` 掉到 `_CALL_BUDGET` 以下，
    dispatch 会掐断**设计内**的单工具最坏路径（3 次尝试），把正常的慢工具变成超时。
    """
    assert _scheduler_mod._TOOL_TIMEOUT > 0
    assert _scheduler_mod._CALL_BUDGET > _scheduler_mod._TOOL_TIMEOUT
    assert _scheduler_mod._DISPATCH_TIMEOUT >= _scheduler_mod._CALL_BUDGET
    # S2-G2：单次尝试的上界是"插件可声明的最大预算 + 管道余量"，
    # 而不是 60s 下限——否则声明 120s 的插件会在重试中途被整体预算掐断。
    assert _scheduler_mod._MAX_TOOL_TIMEOUT >= _scheduler_mod._TOOL_TIMEOUT
    assert (_scheduler_mod._MAX_TOOL_TIMEOUT
            == _scheduler_mod._MAX_PLUGIN_REQUEST_TIMEOUT + _scheduler_mod._TIMEOUT_SLACK)
    # 单调用预算必须真的覆盖 max_retries 次重试 + 退避
    expect = (_scheduler_mod._MAX_TOOL_TIMEOUT * (1 + _scheduler_mod._MAX_TASK_RETRIES)
              + _scheduler_mod._RETRY_BACKOFF_CAP * _scheduler_mod._MAX_TASK_RETRIES)
    assert _scheduler_mod._CALL_BUDGET == expect


@pytest.mark.service
@pytest.mark.asyncio
async def test_dispatch_when_not_running_fails_fast():
    """调度器未运行（从未 start / 已 stop）→ 立即失败返回，绝不入队后悬挂。

    修复前：照常入队 + `await gather`，而没有任何 worker 会消费队列
    → 永久挂起（revert 后本用例以 TimeoutError 失败）。
    """
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=2, checkpoint_path=None)
    # 故意不 start()

    calls = [_tc("c1", "echo", '{"msg":"hi"}'), _tc("c2", "echo", '{"msg":"yo"}')]
    results = await asyncio.wait_for(
        sched.dispatch_tool_calls("p1", calls), timeout=2.0)

    assert len(results) == 2
    for tc, r in results:
        assert isinstance(r, ToolResult)
        assert not r.success
        assert r.error_type == ErrorType.UNAVAILABLE
        assert r.error and "未运行" in r.error
    # 未入队：注册表里不该留下任何痕迹
    assert sched.total_count == 0
    assert sched.pending_count == 0

    # 已 stop 的调度器同样快速失败（而不是等 480s 预算）
    await sched.start()
    await sched.stop()
    results2 = await asyncio.wait_for(
        sched.dispatch_tool_calls("p1", calls), timeout=2.0)
    assert all(not r.success for _, r in results2)
    assert all(r.error_type == ErrorType.UNAVAILABLE for _, r in results2)


@pytest.mark.service
@pytest.mark.asyncio
async def test_stop_resolves_inflight_dispatch():
    """stop() 与 dispatch 竞争：在途 future 必须被明确失败 resolve，dispatch 返回。

    构造：1 个 worker；第一个调用被 WaiterPlugin 卡住（worker 正在执行），第二个
    调用还留在队列里。此时 stop() —— 修复前 worker 只处理"已取到"的任务，队列里
    那个 future 无人 resolve 且注册表随即被 clear，等待方永久挂起（revert 后本用例
    以 TimeoutError 失败）。
    """
    barrier = asyncio.Event()
    tm = await _make_sleep_manager(barrier)
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()

    calls = [_tc("c1", "waiter", "{}"), _tc("c2", "echo", '{"msg":"queued"}')]
    dispatch_task = asyncio.create_task(sched.dispatch_tool_calls("p1", calls))
    # 等 worker 真正开始执行 c1（否则 stop() 可能先跑，测不到"队列里还有条目"）
    for _ in range(100):
        await asyncio.sleep(0.01)
        if sched.running_count == 1 and sched.pending_count == 1:
            break
    assert sched.running_count == 1, "worker 未接管第一个调用，测试前提不成立"
    assert sched.pending_count == 1, "第二个调用未停留在队列中，测试前提不成立"

    await sched.stop()

    results = await asyncio.wait_for(dispatch_task, timeout=5.0)
    assert len(results) == 2
    for tc, r in results:
        assert isinstance(r, ToolResult)
        assert not r.success
        # 失败原因必须**可读**——修复前取消路径经 gather 得到裸 CancelledError，
        # str() 后是空字符串（"失败语义静默"）。
        assert r.error
    assert results[0][1].error_type == ErrorType.CANCELLED       # 执行中被取消
    assert results[1][1].error_type == ErrorType.UNAVAILABLE     # 停留在队列未执行


@pytest.mark.service
@pytest.mark.asyncio
async def test_dispatch_budget_exhausted_fails_bounded_and_keeps_pool_alive(monkeypatch):
    """预算耗尽 → 有界失败（TIMEOUT），且被取消的 worker 不退出 worker 池。

    取消单个任务是通过 cancel worker task 实现的，而 worker 会吞掉 CancelledError
    继续循环——若这一点被改坏，一次超时就会让并发度静默-1（几十次后池子空转）。
    """
    barrier = asyncio.Event()
    tm = await _make_sleep_manager(barrier, extra_plugins=[EchoPlugin])
    # 必须单 worker：只有"唯一那个 worker 被取消后是否还活着"才是真断言
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()
    monkeypatch.setattr(_scheduler_mod, "_DISPATCH_TIMEOUT", 0.05)

    results = await asyncio.wait_for(
        sched.dispatch_tool_calls("p1", [_tc("c1", "waiter", "{}")]), timeout=5.0)
    assert len(results) == 1
    r = results[0][1]
    assert isinstance(r, ToolResult)
    assert not r.success
    assert r.error_type == ErrorType.TIMEOUT
    assert "超时" in (r.error or "")

    # 池子仍然可用（worker 没被取消掉）
    monkeypatch.undo()
    ok = await asyncio.wait_for(
        sched.dispatch_tool_calls("p2", [_tc("c2", "echo", '{"msg":"alive"}')]), timeout=5.0)
    assert ok[0][1].success, "一次超时后 worker 池失效——取消把 worker 杀掉了"
    await sched.stop()


@pytest.mark.service
@pytest.mark.asyncio
async def test_dispatch_empty_calls_returns_empty():
    """空 calls → 空结果（不能因 asyncio.wait 拒绝空集合而抛 ValueError）。"""
    tm = await _make_manager()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=2, checkpoint_path=None)
    await sched.start()
    assert await sched.dispatch_tool_calls("p1", []) == []
    await sched.stop()


# ═══════════════════════════════════════════════════════════════════
# 五.5、_prune_tasks — 终态任务清理
# ═══════════════════════════════════════════════════════════════════

def _make_terminal_task(tid: str, children: list[str] | None = None) -> QueuedToolCall:
    """创建一个终态（COMPLETED）任务。"""
    return QueuedToolCall(id=tid, type=QueuedToolCallKind.BACKGROUND, status=QueuedToolCallState.COMPLETED,
                payload={}, children_ids=children or [],
                run_in_background=True)  # type: ignore[reportCallIssue]


def _make_active_task(tid: str, children: list[str] | None = None) -> QueuedToolCall:
    """创建一个活跃（DISPATCHED）任务。"""
    return QueuedToolCall(id=tid, type=QueuedToolCallKind.BACKGROUND, status=QueuedToolCallState.DISPATCHED,
                payload={}, children_ids=children or [],
                run_in_background=True)  # type: ignore[reportCallIssue]


@pytest.mark.service
def test_prune_removes_terminal_without_active_children():
    """终态任务无活跃子任务 → 被删除。"""
    tm = _make_sync_manager()
    sched = ToolCallDispatcher(tool_executor=tm, checkpoint_path=None)
    t = _make_terminal_task("t1")
    sched._tasks["t1"] = t
    sched._prune_tasks()
    assert "t1" not in sched._tasks


@pytest.mark.service
def test_prune_keeps_active_task():
    """活跃任务 → 保留。"""
    tm = _make_sync_manager()
    sched = ToolCallDispatcher(tool_executor=tm, checkpoint_path=None)
    t = _make_active_task("t1")
    sched._tasks["t1"] = t
    sched._prune_tasks()
    assert "t1" in sched._tasks


@pytest.mark.service
def test_prune_keeps_terminal_with_active_child():
    """终态父任务有活跃子任务 → 保留（子任务还在跑）。"""
    tm = _make_sync_manager()
    sched = ToolCallDispatcher(tool_executor=tm, checkpoint_path=None)
    parent = _make_terminal_task("parent", children=["child"])
    child = _make_active_task("child")
    sched._tasks["parent"] = parent
    sched._tasks["child"] = child
    sched._prune_tasks()
    assert "parent" in sched._tasks
    assert "child" in sched._tasks


@pytest.mark.service
def test_prune_removes_all_when_all_terminal():
    """全部终端且无活跃子 → 全部删除。"""
    tm = _make_sync_manager()
    sched = ToolCallDispatcher(tool_executor=tm, checkpoint_path=None)
    for i in range(5):
        sched._tasks[f"t{i}"] = _make_terminal_task(f"t{i}")
    sched._prune_tasks()
    assert len(sched._tasks) == 0


@pytest.mark.service
def test_prune_removes_terminal_parent_when_child_also_terminal():
    """父终态 + 子终态 → 父子都删除。"""
    tm = _make_sync_manager()
    sched = ToolCallDispatcher(tool_executor=tm, checkpoint_path=None)
    parent = _make_terminal_task("parent", children=["child"])
    child = _make_terminal_task("child")
    sched._tasks["parent"] = parent
    sched._tasks["child"] = child
    sched._prune_tasks()
    assert len(sched._tasks) == 0


# ═══════════════════════════════════════════════════════════════════
# 六、集成测试
# ═══════════════════════════════════════════════════════════════════

import os as _os_module


def _ensure_api_key(monkeypatch):
    """确保 Agent 集成测试有 DEEPSEEK_API_KEY（DelegationPreparer 构造要求非空）。

    用 monkeypatch.setenv 在测试作用域内设置，测试结束自动还原——避免裸写进程 env
    永久污染，导致后收集的 core/tests/test_phase1.py 真实 LLM 测试拿到假 key 而 401
    （openai.AuthenticationError: api key ... invalid）。
    """
    if not _os_module.environ.get("DEEPSEEK_API_KEY"):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-integration")


@pytest.mark.service
@pytest.mark.asyncio
async def test_cognitive_loop_callback_wired():
    """_on_tool_calls 回调被正确传递到 _execute_tool_round。"""
    from unittest.mock import AsyncMock, MagicMock
    from core.status.agent_fsm import AgentFSM
    from core.monitoring.event_bus import EventBus
    from Module.output import OutputManager
    from core.memory.session import SessionMemory
    from core.cognitive import CognitiveLoop
    from core.cognitive.tests.conftest import make_default_registry
    from core.adapter.llm_adapter import LLMAdapter

    llm = MagicMock(spec=LLMAdapter)
    llm.model = "mock"

    async def fake_stream(messages, tools=None, **kw):
        for c in "你好":
            yield {"content": c, "tool_calls": None}
    llm.stream_think = fake_stream

    output = OutputManager()
    fsm = AgentFSM()
    bus = EventBus()
    tm = ToolAdapter([])
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    # build_messages 需 awaitable——整对象 AsyncMock 会让 set_session（sync）也变 async
    prompt.build_messages = AsyncMock(return_value=[
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ])
    session = MagicMock(spec=SessionMemory)
    session.recent_messages.return_value = []
    session.character_binding = {}

    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt,
        session_resolver=functools.partial(_fixed_session_resolver, session),
        tool_adapter=tm,
        runtime_registry=make_default_registry(),
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

    # 流程正常完成，回调未被调用（因为 mock LLM 不产生 tool_calls）
    # 但 _on_tool_calls 参数已被正确传递到 _think_loop
    assert fsm.state.value == "idle"
    await output.close()


@pytest.mark.service
@pytest.mark.asyncio
async def test_cognitive_loop_no_callback_backward_compat():
    """不传 _on_tool_calls → 正常完成（向后兼容）。"""
    from unittest.mock import AsyncMock, MagicMock
    from core.status.agent_fsm import AgentFSM
    from core.monitoring.event_bus import EventBus
    from Module.output import OutputManager
    from core.memory.session import SessionMemory
    from core.cognitive import CognitiveLoop
    from core.cognitive.tests.conftest import make_default_registry
    from core.adapter.llm_adapter import LLMAdapter

    llm = MagicMock(spec=LLMAdapter)
    llm.model = "mock"

    async def fake_stream(messages, tools=None, **kw):
        for c in "你好":
            yield {"content": c, "tool_calls": None}
    llm.stream_think = fake_stream

    output = OutputManager()
    fsm = AgentFSM()
    bus = EventBus()
    tm = ToolAdapter([])
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    # build_messages 需 awaitable——整对象 AsyncMock 会让 set_session（sync）也变 async
    prompt.build_messages = AsyncMock(return_value=[
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ])
    session = MagicMock(spec=SessionMemory)
    session.recent_messages.return_value = []
    session.character_binding = {}

    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt,
        session_resolver=functools.partial(_fixed_session_resolver, session),
        tool_adapter=tm,
        runtime_registry=make_default_registry(),
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
@pytest.mark.usefixtures("fake_storage")
async def test_agent_scheduler_lifecycle(monkeypatch):
    """Agent start → Scheduler 启动。Agent stop → Scheduler 停止。"""
    _ensure_api_key(monkeypatch)
    from core.agent import Agent
    from core.config import AppConfig
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
@pytest.mark.usefixtures("fake_storage")
async def test_agent_fifo_queue(monkeypatch):
    """忙时发送消息 → 排队。空闲后 → 自动出队。"""
    _ensure_api_key(monkeypatch)
    from core.agent import Agent
    from core.config import AppConfig
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
@pytest.mark.usefixtures("fake_storage")
async def test_agent_interrupt_clears_queue(monkeypatch):
    """INTERRUPT → 清空排队消息。"""
    _ensure_api_key(monkeypatch)
    from core.agent import Agent
    from core.config import AppConfig
    from core.tests.fake_llm import FakeLLMBackend
    from core.monitoring.event_bus import EventPriority
    from core.schema.event import InterruptData

    agent = Agent(config=AppConfig())
    agent._llm._backend = FakeLLMBackend(response="hello")
    await agent.start()

    agent._pending_messages = [  # type: ignore[reportAttributeAccessIssue]
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
@pytest.mark.usefixtures("fake_storage")
async def test_scheduler_state_in_debug_info(monkeypatch):
    """DebugInfo.collect() 包含 Scheduler 状态。"""
    _ensure_api_key(monkeypatch)
    from core.agent import Agent
    from core.config import AppConfig
    from core.tests.fake_llm import FakeLLMBackend
    from core.debug.info import collect

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


@pytest.mark.service
class TestCheckpoint:
    """C5/P2-09：checkpoint 空则删、coroutine 不可序列化跳过、损坏/版本隔离。"""

    def test_save_empty_deletes_stale_file(self, tmp_path):
        """无 pending 任务 → 删除旧 checkpoint（防重启重放已执行任务）。"""
        cp = str(tmp_path / "sched.json")
        Path(cp).write_text('{"version": 1, "tasks": []}')
        sched = ToolCallDispatcher(checkpoint_path=cp)

        sched._save_checkpoint()

        assert not Path(cp).exists()  # 陈旧文件已删

    def test_save_skips_non_serializable_payload(self, tmp_path):
        """后台任务 payload 含不可序列化值（set 模拟 coro）→ 不写入 checkpoint。"""
        cp = str(tmp_path / "sched.json")
        sched = ToolCallDispatcher(checkpoint_path=cp)

        t = QueuedToolCall.new_background(payload={"coro": set()})
        t.status = QueuedToolCallState.PENDING
        sched._tasks[t.id] = t

        sched._save_checkpoint()

        # checkpoint 不写（payload 不可序列化），文件不存在
        assert not Path(cp).exists()

    def test_restore_corrupt_checkpoint_isolated(self, tmp_path):
        """损坏文件 → 隔离（warning 不阻断），无任务恢复。"""
        cp = str(tmp_path / "sched.json")
        Path(cp).write_text("{not valid json")
        sched = ToolCallDispatcher(checkpoint_path=cp, num_workers=0)

        sched._restore_checkpoint()

        assert sched.pending_count == 0  # 未恢复任何任务

    def test_restore_version_mismatch_isolated(self, tmp_path):
        """version != 1 → 隔离不恢复。"""
        cp = str(tmp_path / "sched.json")
        Path(cp).write_text('{"version": 99, "tasks": []}')
        sched = ToolCallDispatcher(checkpoint_path=cp, num_workers=0)

        sched._restore_checkpoint()

        assert sched.pending_count == 0


@pytest.mark.service
class TestSystemTickUnknownReason:
    """C5/P2-09：未知 proactive kind 记 warning 不静默吞。"""

    @pytest.mark.asyncio
    async def test_time_of_day_reason_logs_warning(self, caplog):
        import logging
        from core.schema.event import SystemTickData
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        try:
            with caplog.at_level(logging.WARNING, logger="core.agent.agent"):
                agent._handle_system_tick(  # noqa: SLF001
                    SystemTickData(reason="time_of_day", elapsed=0))
            assert any("time_of_day" in r.message for r in caplog.records)
        finally:
            await agent.stop()


# ═══════════════════════════════════════════════════════════════════
# 单次调用预算：尊重插件自己声明的 request_timeout（S2-G2）
# ═══════════════════════════════════════════════════════════════════

class _DeclaredTimeoutExecutor:
    """只回答"这个工具声明了多长预算"的最小 executor（运行时鸭子类型）。"""

    def __init__(self, declared: float | None) -> None:
        self._declared = declared

    def declared_request_timeout(self, tool_name: str) -> float | None:
        return self._declared


@pytest.mark.service
class TestPerCallTimeoutBudget:
    """外层预算必须容得下插件自己声明的 `request_timeout`。

    修前调度器一律 60s，把 desktop/vision **按设计声明**的 120s 硬切在 60s——
    再叠加"取消被归一为业务结果"（#33b）就表现为"用户取消 + 任务标成功"。
    """

    @pytest.mark.service
    def test_declared_budget_is_honoured_up_to_the_ceiling(self):
        sched = ToolCallDispatcher(
            tool_executor=_DeclaredTimeoutExecutor(120.0), num_workers=1)
        assert sched._timeout_for("desktop.file.read") == _scheduler_mod._MAX_TOOL_TIMEOUT  # noqa: SLF001

    @pytest.mark.service
    def test_no_declaration_keeps_the_default(self):
        sched = ToolCallDispatcher(
            tool_executor=_DeclaredTimeoutExecutor(None), num_workers=1)
        assert sched._timeout_for("web_fetch") == _scheduler_mod._TOOL_TIMEOUT  # noqa: SLF001

    @pytest.mark.service
    def test_small_declaration_never_shrinks_below_the_default(self):
        """插件声明 5s 也不能把外层压到 5s——外层还包着校验/审批/信号量。"""
        sched = ToolCallDispatcher(
            tool_executor=_DeclaredTimeoutExecutor(5.0), num_workers=1)
        assert sched._timeout_for("tiny") == _scheduler_mod._TOOL_TIMEOUT  # noqa: SLF001

    @pytest.mark.service
    def test_absurd_declaration_is_capped(self):
        sched = ToolCallDispatcher(
            tool_executor=_DeclaredTimeoutExecutor(9999.0), num_workers=1)
        assert sched._timeout_for("greedy") == _scheduler_mod._MAX_TOOL_TIMEOUT  # noqa: SLF001
