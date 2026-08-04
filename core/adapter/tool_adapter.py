"""ToolAdapter — 封装工具子系统的创建、加载、生命周期管理。

实现 ToolExecutor Protocol，委托给 ToolManager。
消费者通过此适配器获取 ToolExecutor + ToolContext，
不再直接依赖 Module/tool 的类型。
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, TYPE_CHECKING

from Module.tool import ToolManager, PluginContext, ToolContext
from Module.tool.protocol import ToolResult, ToolEmitter
from core.schema.error_type import ErrorType  # noqa: F401 — 由 __init__.py 重导出

if TYPE_CHECKING:
    from core.config import AppConfig
    from Module.output import OutputManager

logger = logging.getLogger(__name__)


class ToolAdapter:
    """封装工具子系统：创建、加载、生命周期、类型导出。

    实现 ToolExecutor Protocol，委托给 ToolManager。

    用法:
        adapter = ToolAdapter(ALL_PLUGINS, session_id="...", user_name="...", config=...)
        await adapter.start(extra_services={...})
        # ... 使用 adapter.execute() / adapter.definitions() ...
        await adapter.shutdown()
    """

    def __init__(
        self,
        plugins: list[type],
        *,
        session_id: str = "",
        user_name: str = "",
        config: "AppConfig | None" = None,
        services: dict[str, Any] | None = None,
        hitl_broadcast: "OutputManager | None" = None,
        desktop_bridge: Any = None,
    ):
        """初始化工具适配器。

        Args:
            plugins: 工具插件类型列表（如 ALL_PLUGINS）。
            session_id: 会话 ID，用于 ToolContext。
            user_name: 用户名，用于 ToolContext.user_id。
            config: 应用配置，传入 PluginContext 和 ToolContext。
            services: 初始服务字典（运行时依赖注入）。
            hitl_broadcast: HITL 审批广播输出管理器。
            desktop_bridge: DesktopToolBridge 实例（构造绑定，definitions() D-j
                过滤用——desktop 工具是否暴露给 LLM）。None 时 desktop 工具一律过滤。
        """
        self._plugins = plugins
        self._session_id = session_id
        self._user_name = user_name
        self._config = config
        self._services = services or {}
        self._hitl_broadcast = hitl_broadcast
        self._desktop_bridge = desktop_bridge
        self._started = False

        # 创建 ToolManager（构造时不加载插件）
        self._manager = ToolManager()

        # 创建 PluginContext（session_id 在 start() 中更新）
        self._plugin_ctx = PluginContext(
            session_id=session_id,
            config=config,
        )

        # 构建 ToolContext（services 在 start() 中填充）
        self._tool_ctx = ToolContext(
            session_id=session_id,
            user_id=user_name,
            services=dict(self._services),
        )

    # ---- 生命周期 ----

    async def start(self, extra_services: dict[str, Any] | None = None) -> None:
        """加载所有插件、设置 HITL broadcast、构建 ToolContext。

        extra_services 合并到 tool_context.services（运行时依赖注入）。
        幂等：重复调用 no-op。

        Args:
            extra_services: 额外的运行时服务，合并到 tool_context.services。
        """
        if self._started:
            return

        # 合并运行时服务
        if extra_services:
            self._tool_ctx.services.update(extra_services)

        # 更新 PluginContext
        self._plugin_ctx.session_id = self._session_id
        self._plugin_ctx.memory = self._tool_ctx.services.get("memory")
        self._plugin_ctx.scheduler = self._tool_ctx.services.get("scheduler")

        # 加载插件
        await self._manager.load_all(self._plugins, self._plugin_ctx)

        # 注入 ToolAdapter 管理的服务
        self._tool_ctx.services["_tool_manager"] = self._manager
        self._tool_ctx.services["_tool_names"] = list(self._manager.tool_names)
        self._tool_ctx.services["_tool_count"] = len(self._manager._plugins)

        # 设置 HITL broadcast
        if self._hitl_broadcast is not None:
            self._manager.set_hitl_broadcast(self._hitl_broadcast)

        self._started = True
        logger.info("ToolAdapter: %d 个工具已加载", len(self._manager._plugins))

    async def shutdown(self) -> None:
        """关闭所有插件。委托给 ToolManager.shutdown()。"""
        await self._manager.shutdown()

    # ---- 属性 ----

    @property
    def tool_context(self) -> ToolContext:
        """运行时 ToolContext。

        返回同一个 mutable 对象，services 的动态更新
        （_tool_names、_fsm_state 等）对所有消费者立即可见。
        """
        return self._tool_ctx

    # ---- ToolExecutor Protocol ----

    async def execute(self, name: str, *, ctx: ToolContext, **raw_params: Any) -> ToolResult:
        """执行工具。委托给 ToolManager.execute()。

        Args:
            name: 工具名称。
            ctx: 工具执行上下文。
            **raw_params: 工具参数（键值对）。

        Returns:
            ToolResult: 工具执行结果。
        """
        return await self._manager.execute(name, ctx=ctx, **raw_params)

    async def execute_stream(
        self, name: str, *, ctx: ToolContext, emit: ToolEmitter, **raw_params: Any
    ) -> AsyncIterator[Any]:
        """流式执行工具。委托给 ToolManager.execute_stream()。

        Args:
            name: 工具名称。
            ctx: 工具执行上下文。
            emit: 流式事件发射器。
            **raw_params: 工具参数（键值对）。

        Returns:
            AsyncIterator[Any]: 流式工具的异步事件迭代器。
        """
        async for event in self._manager.execute_stream(name, ctx=ctx, emit=emit, **raw_params):
            yield event

    def definitions(self, format: str = "openai") -> list[dict]:
        """返回已注册工具的 LLM function schema 列表。

        D-j 过滤：desktop 工具仅在 desktop_bridge 存在且可用时暴露给 LLM——
        未连接 desktop 客户端时从 schema 移除，避免 LLM 选不可用工具。

        Args:
            format: 输出格式（默认 "openai"）。

        Returns:
            list[dict]: OpenAI function-calling 格式的工具定义列表。
        """
        filter_runtime = (
            lambda rt: rt != "desktop"
            or (self._desktop_bridge is not None and self._desktop_bridge.is_available())
        )
        return self._manager.definitions(format, filter_runtime=filter_runtime)

    async def cancel_all(self) -> None:
        """打断所有运行中的工具。"""
        await self._manager.cancel_all()

    def execute_now(self) -> list[str]:
        """返回可立即执行的工具名列表。

        Returns:
            list[str]: 已注册工具的名称列表。
        """
        return list(self._manager.tool_names)
