"""ToolAdapter 测试。"""
from __future__ import annotations

import pytest

from Module.tool.protocol import ToolResult, ToolContext, ToolPlugin
from Module.tool.safety import SafetyProfile


class _MockPlugin(ToolPlugin):
    """测试用插件 — 注册后 execute() 返回 ok。"""
    name = "mock_tool"
    description = "A mock tool for testing"
    safety = SafetyProfile()

    async def execute(self, *, ctx, params=None):
        return ToolResult.ok(name=self.name, content="mock result", duration_ms=1.0)


# ---------------------------------------------------------------------------
# 构造函数
# ---------------------------------------------------------------------------

class TestConstruction:
    """ToolAdapter 构造和启动前行为。"""

    @pytest.mark.service
    def test_definitions_empty_before_start(self):
        """start() 前 definitions() 返回空列表。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        assert adapter.definitions() == []

    @pytest.mark.service
    def test_tool_context_available_before_start(self):
        """start() 前 tool_context 属性可用，services 已注入。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(
            plugins=[_MockPlugin],
            session_id="test-sid",
            user_name="tester",
        )
        ctx = adapter.tool_context
        assert isinstance(ctx, ToolContext)
        assert ctx.session_id == "test-sid"
        assert ctx.user_id == "tester"


# ---------------------------------------------------------------------------
# start()（启动）
# ---------------------------------------------------------------------------

class TestStart:
    """start() 行为。"""

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_start_loads_plugins(self):
        """start() 后 definitions() 返回已注册工具的 schema。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        defs = adapter.definitions()
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "mock_tool"
        await adapter.shutdown()

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        """重复 start() 无副作用 — definitions() 不会翻倍。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        await adapter.start()
        defs = adapter.definitions()
        assert len(defs) == 1
        await adapter.shutdown()

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_start_with_extra_services(self):
        """start(extra_services={...}) 合并到 tool_context.services。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(
            plugins=[_MockPlugin],
            session_id="sid-1",
            user_name="test-user",
            services={"memory": "mock_memory", "scheduler": "mock_scheduler"},
        )
        await adapter.start(extra_services={
            "session": "mock_session",
            "_started_at": 1234567890.0,
        })
        ctx = adapter.tool_context
        assert ctx.services.get("memory") == "mock_memory"
        assert ctx.services.get("scheduler") == "mock_scheduler"
        assert ctx.services.get("session") == "mock_session"
        assert ctx.services.get("_started_at") == 1234567890.0
        # ToolAdapter 自动注入的 key
        assert "_tool_manager" in ctx.services
        assert "_tool_names" in ctx.services
        assert "_tool_count" in ctx.services
        await adapter.shutdown()


# ---------------------------------------------------------------------------
# execute()（执行）
# ---------------------------------------------------------------------------

class TestExecute:
    """execute() 委托给 ToolManager。"""

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_execute_delegates_to_manager(self):
        """execute() 调用 ToolManager.execute() 并返回 ToolResult。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        result = await adapter.execute(
            name="mock_tool",
            ctx=adapter.tool_context,
        )
        assert result.success is True
        assert result.content == "mock result"
        await adapter.shutdown()

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        """未注册工具返回失败结果。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        result = await adapter.execute(
            name="nonexistent",
            ctx=adapter.tool_context,
        )
        assert result.success is False
        await adapter.shutdown()


# ---------------------------------------------------------------------------
# 其他 ToolExecutor 方法
# ---------------------------------------------------------------------------

class TestToolExecutorMethods:
    """覆盖 definitions()、cancel_all()、execute_now() 等方法。"""

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_definitions_returns_openai_format(self):
        """definitions() 返回 OpenAI function-calling 格式。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        defs = adapter.definitions(format="openai")
        assert isinstance(defs, list)
        assert defs[0]["type"] == "function"
        await adapter.shutdown()

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_cancel_all_does_not_raise(self):
        """cancel_all() 无异常。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        await adapter.cancel_all()
        await adapter.shutdown()

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_execute_now_returns_list(self):
        """execute_now() 返回注册工具名列表。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        names = adapter.execute_now()
        assert isinstance(names, list)
        assert "mock_tool" in names
        await adapter.shutdown()


# ---------------------------------------------------------------------------
# shutdown()（关闭）
# ---------------------------------------------------------------------------

class TestShutdown:
    """shutdown() 行为。"""

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_shutdown_does_not_raise(self):
        """shutdown() 不抛异常，二次调用安全。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        await adapter.start()
        await adapter.shutdown()
        await adapter.shutdown()  # 二次调用不抛异常


# ---------------------------------------------------------------------------
# submit_approval()（HITL 审批回传）
# ---------------------------------------------------------------------------

class TestSubmitApproval:
    """P1-6：ToolAdapter 必须公开 submit_approval。

    背景：`AgentInfra.tool_registry` 生产注入的是 **ToolAdapter**，而
    `Module/workflow_engine/lifecycle.py::submit_approval` 经
    `hasattr(tool_registry, "submit_approval")` 判定后委托——ToolAdapter 缺该方法时
    `WorkflowEngine.submit_approval` 恒返回 False（死路径）。同时 input 层原先直接访问
    `agent.tool_adapter._manager`（`# noqa: SLF001`），违反「跨模块禁止私有访问」。
    本方法把委托收口为公开 API：既让 WorkflowEngine 的判定在真实依赖下成立，
    也供 ws_handler 走公开接口。
    """

    @pytest.mark.service
    def test_exposes_public_submit_approval(self):
        """ToolAdapter 公开 submit_approval（而非只存在于私有 _manager 上）。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        assert callable(getattr(adapter, "submit_approval", None))

    @pytest.mark.service
    def test_delegates_to_tool_manager(self):
        """委托给 ToolManager.submit_approval，透传 request_id/approved 与返回值。"""
        from unittest.mock import MagicMock

        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        adapter._manager.submit_approval = MagicMock(return_value=True)  # noqa: SLF001

        assert adapter.submit_approval("req_abc", True) is True
        adapter._manager.submit_approval.assert_called_once_with(  # noqa: SLF001
            "req_abc", True
        )

    @pytest.mark.service
    def test_returns_false_for_unknown_request(self):
        """未知/过期 request_id → False（不抛异常，由调用方记日志）。"""
        from core.adapter.tool_adapter import ToolAdapter
        adapter = ToolAdapter(plugins=[_MockPlugin])
        assert adapter.submit_approval("req_never_existed", True) is False

