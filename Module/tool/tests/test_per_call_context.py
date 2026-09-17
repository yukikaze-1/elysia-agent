"""Per-call ToolContext 与 call-ID 任务跟踪测试（C2/P1-08/P1-09/P1-13，marker=unit）。"""

from __future__ import annotations

import asyncio

import pytest

from Module.tool.manager import ToolManager
from Module.tool.protocol import (
    ErrorType,
    PluginContext,
    ToolContext,
    ToolPlugin,
    ToolResult,
)

pytestmark = pytest.mark.unit


class TestCloneForCall:
    """clone_for_call：services 浅拷贝隔离 + session/call 绑定。"""

    def test_services_isolated_between_calls(self):
        """P1-09：调用 A 写 _safe_path 不污染调用 B（per-call 副本）。"""
        base = ToolContext(session_id="default")
        emit = object()
        base.services["_emit"] = emit
        a = base.clone_for_call(call_id="call_a", session_id="sess_a")
        b = base.clone_for_call(call_id="call_b", session_id="sess_b")

        a.services["_safe_path"] = "/sandbox/a.txt"

        assert b.services.get("_safe_path") is None  # 不共享
        assert a.services["_safe_path"] == "/sandbox/a.txt"
        # 内部服务对象共享（浅拷贝语义），dict 本身独立
        assert a.services["_emit"] is emit

    def test_binds_session_and_call_id(self):
        """P1-08：session_id 绑定请求 conversationId，call_id 唯一。"""
        base = ToolContext(session_id="old")
        clone = base.clone_for_call(call_id="call_1", session_id="sess_x")
        assert clone.session_id == "sess_x"
        assert clone.call_id == "call_1"
        # 未指定 session → 继承 base
        clone2 = base.clone_for_call(call_id="call_2")
        assert clone2.session_id == "old"


class _SleepyPlugin(ToolPlugin):
    """execute 阻塞在 event 上的假插件（测任务跟踪/取消）。"""

    name = "sleepy"
    description = "test"
    capabilities = frozenset()

    async def setup(self, ctx: PluginContext) -> None:  # noqa: ARG002
        return None

    async def teardown(self, ctx: PluginContext) -> None:  # noqa: ARG002
        return None

    async def execute(self, ctx: ToolContext, **params) -> ToolResult:
        gate = ctx.services.get("_gate")
        if gate is not None:
            await gate.wait()
        return ToolResult.ok(name=self.name, content="done", duration_ms=0.0)


async def _make_manager() -> ToolManager:
    tm = ToolManager()
    await tm.load_all([_SleepyPlugin], PluginContext())
    return tm


class TestCallIdTaskTracking:
    """P1-13：同名工具多个调用按 call_id 独立跟踪，不互相覆盖。"""

    @pytest.mark.asyncio
    async def test_same_name_tasks_tracked_separately(self):
        """两个同名调用（不同 call_id）→ _tasks 有两项，取消只影响各自。"""
        tm = await _make_manager()
        gate = asyncio.Event()

        async def run(call_id: str):
            ctx = ToolContext(session_id="s", call_id=call_id)
            ctx.services["_gate"] = gate
            return await tm.execute("sleepy", ctx=ctx)

        t1 = asyncio.create_task(run("call_1"))
        t2 = asyncio.create_task(run("call_2"))
        await asyncio.sleep(0.05)  # 让任务启动
        # 两个任务都在跟踪中（call_id 各自记录）
        assert set(tm._tasks.keys()) == {"call_1", "call_2"}  # noqa: SLF001

        gate.set()
        await asyncio.wait([t1, t2])
        assert t1.result().success and t2.result().success
        assert tm._tasks == {}  # noqa: SLF001 — 完成后清空

    @pytest.mark.asyncio
    async def test_cancel_all_cancels_every_call(self):
        """cancel_all 取消全部 call_id 任务（不再只取消一个同名）。"""
        tm = await _make_manager()
        gate = asyncio.Event()

        async def run(call_id: str):
            ctx = ToolContext(session_id="s", call_id=call_id)
            ctx.services["_gate"] = gate
            return await tm.execute("sleepy", ctx=ctx)

        t1 = asyncio.create_task(run("call_1"))
        t2 = asyncio.create_task(run("call_2"))
        await asyncio.sleep(0.05)
        assert len(tm._tasks) == 2  # noqa: SLF001

        await tm.cancel_all()
        # 两个任务都被取消。**归一为结果的位置是 manager（子任务的拥有者）**，
        # 不是 safety——后者是子任务，吞掉外部取消会让外层 wait_for 失去超时语义
        # （S2-R2-1）。这里的语义是"调用方没被取消、只是我 cancel 了自己的子任务"。
        await asyncio.wait([t1, t2], timeout=1.0)
        for task in (t1, t2):
            result = task.result()
            assert result.success is False
            assert result.error_type is ErrorType.CANCELLED
        assert t2.result().success is False
        assert "取消" in (t1.result().error or "")
        assert tm._tasks == {}  # noqa: SLF001


class TestSafetyPerCallContext:
    """SafetyPipeline 的 _safe_path 写 per-call ctx（P1-09 隔离）。"""

    def test_safe_path_written_to_invocation_ctx_only(self):
        """规范化 path 只写本次调用 ctx，不写共享 ctx。"""
        # 直接验证 clone_for_call 语义在 safety 路径下的效果：
        # 两次调用各自 ctx，A 的 _safe_path 不被 B 覆盖
        base = ToolContext(session_id="default")
        a = base.clone_for_call(call_id="a", session_id="s1")
        b = base.clone_for_call(call_id="b", session_id="s1")

        a.services["_safe_path"] = "/safe/a.txt"
        b.services["_safe_path"] = "/safe/b.txt"

        assert a.services["_safe_path"] == "/safe/a.txt"
        assert b.services["_safe_path"] == "/safe/b.txt"
        assert base.services.get("_safe_path") is None  # 共享 ctx 无残留
