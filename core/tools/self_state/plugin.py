"""自我感知工具插件 — agent 查看自身状态。"""

from typing import Any

from Module.tool.protocol import ToolPlugin, ToolContext, ToolEmitter
from Module.tool.safety import SafetyProfile


class SelfStatePlugin(ToolPlugin):
    name = "get_self_state"
    description = "查看 agent 的当前运行状态：FSM 状态、会话信息、工具使用情况"
    param_model = None
    capabilities: set = set()
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: Any = None,
    ) -> str:
        """查看 agent 的当前运行状态。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 未使用（此工具不需要参数）。

        Returns:
            agent 运行状态字符串。
        """
        fsm_state = ctx.get("_fsm_state", "unknown")
        session_count = ctx.get("_session_msg_count", 0)
        tool_count = ctx.get("_tool_count", 0)
        trace_id = ctx.trace_id or "N/A"
        agent_id = ctx.agent_id or "default"

        lines = [
            f"Agent ID: {agent_id}",
            f"当前 Trace ID: {trace_id}",
            f"FSM 状态: {fsm_state}",
            f"会话消息数: {session_count}",
            f"已加载工具数: {tool_count}",
        ]
        return "\n".join(lines)
