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
        """关闭所有插件。委托给 ToolManager.shutdown()。

        C4/P1-07：shutdown 后复位 `_started`——同一 Agent 实例 restart 时
        start() 能完整重新注册 plugin/schema/registry（修复前 _started 恒 True，
        再次 start 提前返回，工具为空）。
        """
        await self._manager.shutdown()
        self._started = False

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

    async def cancel_all(self, *, drain_timeout: float = 0.0) -> int:
        """打断所有运行中的工具。

        Args:
            drain_timeout: >0 时在取消后等待落定（总预算，秒）——关机路径传正数，
                避免插件 teardown 与仍在跑的协程并发关闭共享资源（ARCH-INC-P2-03）。

        Returns:
            超时前未落定的任务数。
        """
        return await self._manager.cancel_all(drain_timeout=drain_timeout)

    def submit_approval(self, request_id: str, approved: bool,
                        approver_connection_id: str | None = None) -> bool:
        """回传 HITL 审批结果（公开 API，委托 ToolManager → SafetyPipeline）。

        P1-6：`Module/workflow_engine/lifecycle.py::submit_approval` 经
        `hasattr(tool_registry, "submit_approval")` 判定后委托，而生产注入
        `AgentInfra.tool_registry` 的正是本类——缺此方法会让
        `WorkflowEngine.submit_approval` 恒返回 False（死路径）。input 层也因此
        原先直接访问 `agent.tool_adapter._manager`（`# noqa: SLF001`）。此处把委托
        收口为公开方法：纵深防御，且消除跨模块私有访问。

        Args:
            request_id: 审批请求 ID（即 HITL 广播的 request_id）。
            approved: True 批准，False 拒绝。
            approver_connection_id: 提交审批的连接 ID（CR-13 绑定发起连接）。

        Returns:
            bool: True 找到并处理；False 请求不存在（可能已超时或从未创建）。
        """
        return self._manager.submit_approval(
            request_id, approved, approver_connection_id)

    def execute_now(self) -> list[str]:
        """返回可立即执行的工具名列表（`ToolRegistryProtocol` 的历史命名）。

        Returns:
            list[str]: 已注册工具的名称列表。
        """
        return self.tool_names

    def resolve_llm_name(self, llm_name: str) -> str | None:
        """LLM 返回函数名 → 运行时工具名（desktop_file_select → desktop.file.select）。

        definitions() 暴露脱点名（OpenAI/Anthropic/Gemini 禁点号），LLM 会原样回传
        脱点名；执行/审批/落库需要运行时名，消费端反查。非本管理器工具返回 None。

        Args:
            llm_name: LLM 产出的 function.name。

        Returns:
            对应运行时工具名；未知（submit_task/幻觉名）返回 None。
        """
        return self._manager.resolve_llm_name(llm_name)

    def runtime_for(self, tool_name: str) -> str:
        """按运行时工具名反查 runtime（backend/desktop/local_worker）；未知 → backend（D6-03）。

        Args:
            tool_name: 运行时工具名（resolve_llm_name 结果）。

        Returns:
            ToolRuntime 值（"backend"/"desktop"/"local_worker"）；submit_task/未知 → "backend"。
        """
        runtime = self._manager.runtime_of(tool_name)
        return runtime if runtime in ("backend", "desktop", "local_worker") else "backend"

    # ---- 管理门面（批次 A / ARCH-INC-P2-04）----
    #
    # 背景：输入层与 debug 层此前**直接穿透** `ToolAdapter._manager`（9 处私有访问），
    # 读 `_plugins` / `_build_param_summary` / `plugin_list` 等内部结构。评估
    # ARCH-INC-P2-04 的处置是「先补门面，内部拆分暂缓」——本段即该门面：
    # 下游只依赖这些方法，`ToolManager` 内部怎么拆都不再影响调用方。
    #
    # 门面**不做转发之外的加工**，以保证行为逐字节不变（评估 §5 批次 A 第 5 条：
    # 不改变任何 wire 或运行时行为）。

    def declared_request_timeout(self, tool_name: str) -> float | None:
        """插件声明的单次执行预算（透传 manager；调度器预算用，S2-G2）。"""
        return self._manager.declared_request_timeout(tool_name)

    @property
    def tool_names(self) -> list[str]:
        """已注册工具名列表。

        `ToolManager.tool_names` 的 docstring 写明该属性是「保留旧接口兼容性 —
        server.py health endpoint 使用」，但 health 探针此前经 `_manager` 私有
        穿透读取（P1-02 残留之一）。此处收口为公开门面，与 `execute_now()`
        同源（后者是 `ToolRegistryProtocol` 的历史命名）。
        """
        return list(self._manager.tool_names)

    @property
    def plugin_list(self) -> list[dict]:
        """工具面板列表（含 runtime/version/risk_level/available 等派生字段）。"""
        return self._manager.plugin_list

    @property
    def executing_now(self) -> list[str]:
        """当前正在执行的工具名列表。"""
        return self._manager.executing_now

    @property
    def history(self) -> list[dict]:
        """工具执行历史（最近若干条，含 success/error/duration_ms）。"""
        return self._manager.history

    @property
    def capability_status(self) -> dict[str, bool]:
        """工具能力声明汇总（能力名 → 是否有插件声明）。"""
        return self._manager.capability_status

    def registry_metadata(self, *, desktop_available: bool = False) -> list[Any]:
        """完整注册表元数据（BE-042）。

        Args:
            desktop_available: desktop 运行时是否可用；false 时 desktop 工具仍列出
                但 available=false。

        Returns:
            ToolMetadata 列表。
        """
        return self._manager.registry_metadata(desktop_available=desktop_available)

    def set_desktop_available(self, available: bool) -> None:
        """同步 desktop 运行时可用性（bridge 连接/断开时调用）。

        Args:
            available: desktop 客户端是否已连接可执行。
        """
        self._manager.set_desktop_available(available)

    def get_pending_approvals(self) -> list[tuple[str, str, dict]]:
        """待审批请求列表（WS 重连时重新广播用）。

        Returns:
            [(request_id, tool_name, params), ...]。
        """
        return self._manager.get_pending_approvals()

    async def reload_plugin(self, name: str) -> tuple[bool, str]:
        """热重载单个工具插件（原子替换 + 失败回滚）。

        Args:
            name: 要重载的工具名。

        Returns:
            (ok, reason)；ok=False 时 reason 为失败原因。
        """
        return await self._manager.reload_plugin(name)

    def build_approval_payload(
        self, request_id: str, tool_name: str, params: dict, *, enrich: bool = False,
    ) -> dict[str, Any]:
        """构造待审批请求的 wire payload（v1 基础 / v2 富化）。

        payload 形状属于工具子系统，故不下放给传输层拼装；详见
        `ToolManager.build_approval_payload`。

        Args:
            request_id: 审批请求 ID。
            tool_name: 工具名。
            params: 工具原始参数。
            enrich: True 时附 v2 富化字段。

        Returns:
            可直接作为 `command="tool_approval"` 的 params 送出的 dict。
        """
        return self._manager.build_approval_payload(
            request_id, tool_name, params, enrich=enrich)
