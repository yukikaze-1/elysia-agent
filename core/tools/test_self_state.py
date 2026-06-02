"""SelfStatePlugin 单元测试。"""

import pytest

from core.tools.self_state import SelfStatePlugin
from Module.tool.protocol import ToolContext


class TestSelfStatePlugin:
    """SelfStatePlugin 的测试用例。"""

    @pytest.fixture
    def plugin(self) -> SelfStatePlugin:
        return SelfStatePlugin()

    # ---- 默认上下文 ----

    @pytest.mark.asyncio
    async def test_default_context_shows_placeholders(
        self, plugin: SelfStatePlugin,
    ) -> None:
        """空 ToolContext、无 services 时，输出包含占位符。"""
        ctx = ToolContext()
        result = await plugin.execute(ctx=ctx)

        assert "Agent ID: default" in result
        assert "FSM 状态: unknown" in result
        assert "当前 Trace ID: N/A" in result
        assert "会话消息数: 0" in result
        assert "已加载工具数: 0" in result

    # ---- 完整上下文 ----

    @pytest.mark.asyncio
    async def test_full_context_shows_all_values(
        self, plugin: SelfStatePlugin,
    ) -> None:
        """当 ToolContext 包含 trace_id、agent_id 和 services 时，输出反映所有值。"""
        ctx = ToolContext(
            trace_id="trace-123",
            agent_id="agent-7",
            services={
                "_fsm_state": "thinking",
                "_session_msg_count": 42,
                "_tool_count": 5,
            },
        )
        result = await plugin.execute(ctx=ctx)

        assert "Agent ID: agent-7" in result
        assert "当前 Trace ID: trace-123" in result
        assert "FSM 状态: thinking" in result
        assert "会话消息数: 42" in result
        assert "已加载工具数: 5" in result

    # ---- 默认 agent_id ----

    def test_default_agent_id_when_not_provided(
        self, plugin: SelfStatePlugin,
    ) -> None:
        """ToolContext 创建时不传 agent_id，默认值为 "default"。"""
        ctx = ToolContext()
        assert ctx.agent_id == "default"
