"""Agent 生命周期 — 工具/调度/空闲监控 启停。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast

from core.monitoring.event_bus import EventBus, EventPriority, EventType
from core.status.agent_fsm import AgentFSM, AgentState
from core.schema.event import (
    InterruptData, UserInputData, TaskResultData, StateChangeData, SystemTickData,
)
# 运行时导入：cast(WebSocketChannel, target) 的实参类型名须在运行时解析
# （Module.output 不依赖 core，无环）
from Module.output import WebSocketChannel

if TYPE_CHECKING:
    from core.task import TaskScheduler
    from core.memory.long_term import LongTermMemory
    from core.config import AppConfig
    from core.memory.session import SessionMemory
    from core.monitoring.token_tracker import TokenTracker
    from Module.tts.protocol import TTSBackend
    from core.status import AgentStatus, CognitivePhaseController
    from core.adapter.tool_adapter import ToolAdapter
    from core.monitoring.idle_monitor import IdleMonitor
    from core.cognitive import CognitiveLoop
    from core.desktop.bridge import DesktopToolBridge
    from Module.output import OutputManager

logger = logging.getLogger(__name__)


class _LifecycleTools:
    """工具/调度/空闲监控 生命周期 mixin。"""

    if TYPE_CHECKING:
        _tool_adapter: ToolAdapter
        _long_term: LongTermMemory
        _scheduler: TaskScheduler
        _config: AppConfig
        session: SessionMemory
        _started_at: float
        _token_tracker: TokenTracker
        _bus: EventBus
        status: AgentStatus
        phases: CognitivePhaseController
        _fsm: AgentFSM
        _cognitive: CognitiveLoop
        _embed_task: asyncio.Task[Any] | None
        _idle: IdleMonitor
        _event_task: asyncio.Task[None] | None
        _process_events: Callable[[], Coroutine[Any, Any, None]]
        _desktop_bridge: DesktopToolBridge
        _output: OutputManager

    async def _start_tool_context(self) -> None:
        """启动 ToolAdapter，注入运行时服务 + 绑定 desktop bridge deliver 回调。

        注入双轨（spec §8 review 修正，两轨定死）：
        ① **构造绑定**——desktop_bridge 在 Agent.__init__ 创建并传入 ToolAdapter
           （definitions() D-j 过滤用，此处不建桥）；
        ② **execute 运行时注入**——`start(extra_services={"desktop_bridge": bridge})`
           经 ToolContext.services 供 desktop 插件 `ctx.services.get("desktop_bridge")`。
        """
        extra_services = {
            "memory": self._long_term,
            "scheduler": self._scheduler,
            "config": self._config,
            "session": self.session,
            "_bus": self._bus,
            "_started_at": self._started_at,
            "_token_tracker": self._token_tracker,
            "_fsm_state": self.status.current_name(),
            "_session_msg_count": len(self.session.messages),
        }
        bridge = getattr(self, "_desktop_bridge", None)
        if bridge is not None:
            # deliver 回调：bridge 定向投递 tool.request 到 electron 通道
            bridge.bind_deliver(self._deliver_tool_request)
            extra_services["desktop_bridge"] = bridge
        await self._tool_adapter.start(extra_services=extra_services)
        self._cognitive.configure(tool_adapter=self._tool_adapter)
        self._scheduler.set_tool_adapter(self._tool_adapter)

    async def _deliver_tool_request(self, frame: dict) -> None:
        """Desktop 工具请求定向投递（bridge._deliver 回调，spec §5.2）。

        bridge 已构造 `ToolRequestMessage(...).wire_dict()`（camelCase 帧），此处只负责
        定位目标连接：遍历 `agent.output._channels` 找 `client_platform == "electron"`
        的 WebSocketChannel → `_send_json(frame)`（定向单连接，非广播）。
        无目标连接记 warning——插件层已提前拦截（is_available 守卫），此为兜底。

        Args:
            frame: tool.request wire 帧（camelCase dict）。
        """
        target = None
        for ch in self._output._channels:  # noqa: SLF001 — 组合根内深度遍历通道
            if getattr(ch, "client_platform", "") == "electron":
                target = ch
                break
        if target is None:
            logger.warning(
                "DesktopToolBridge 投递：无 electron 目标连接，tool.request 丢弃")
            return
        # _send_json 仅 WebSocketChannel 定义（OutputChannel ABC 无此方法），
        # 已按 client_platform == "electron" 过滤，运行时必为 WebSocketChannel
        await cast(WebSocketChannel, target)._send_json(frame)  # noqa: SLF001 — 定向投递桌面帧

    async def _start_worker_pool(self) -> None:
        """启动 Worker Pool + embedding 预加载后台任务。"""
        await self._scheduler.start()
        self._embed_task = asyncio.create_task(
            self._long_term._embedding_provider.ensure(), name="embedding-preload")

    async def _start_idle_and_events(self) -> None:
        """启动 IdleMonitor + 主事件循环（独立 asyncio.Task）。"""
        await self._idle.start()
        self._event_task = asyncio.create_task(self._process_events())

    async def _stop_event_task(self) -> None:
        """停止 _process_events 事件循环——防止它在 shutdown 期间继续 post 新任务。"""
        if self._event_task:
            logger.debug("Cancelling event task...")
            self._event_task.cancel()
            try:
                await asyncio.wait_for(self._event_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._event_task = None

    def _stop_cognitive_and_tts(self) -> None:
        """Cancel 当前 cognitive task + TTS 任务（belt-and-suspenders）。"""
        self._cognitive.mark_shutting_down()  # 门禁——after_response 检查后提前退出
        self._cognitive.cancel_current()
        self._cognitive._tts_mgr.cancel_all()

    async def _stop_embedding_if_running(self) -> None:
        """取消并等待 embedding 预加载任务完成。"""
        if self._embed_task and not self._embed_task.done():
            self._embed_task.cancel()
        # 等待 task 完成——cancel 后仍需 await 消耗 CancelledError
        if self._embed_task:
            try:
                await asyncio.wait_for(self._embed_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    async def _stop_scheduler(self) -> None:
        """停止 Scheduler Worker Pool。"""
        logger.debug("Stopping scheduler Worker Pool...")
        await self._scheduler.stop()

    async def _stop_tool_adapter(self) -> None:
        """关闭 ToolAdapter（清理所有工具插件）。"""
        logger.debug("Stopping ToolAdapter...")
        await self._tool_adapter.shutdown()

    async def _stop_idle(self) -> None:
        """停止 IdleMonitor。"""
        logger.debug("Stopping idle monitor...")
        await self._idle.stop()
