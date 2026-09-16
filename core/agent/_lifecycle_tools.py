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
from Module._common.task_scope import AsyncTaskScope, cancel_and_drain
# 运行时导入：cast(WebSocketChannel, target) 的实参类型名须在运行时解析
# （Module.output 不依赖 core，无环）
from Module.output import WebSocketChannel

if TYPE_CHECKING:
    from core.tool_runtime import ToolCallDispatcher
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
        _scheduler: ToolCallDispatcher
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
        _lifecycle_tasks: AsyncTaskScope
        _turns: AsyncTaskScope
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
            # Task 7（记忆 v2 隔离）：不再裸注入全局 _long_term 单例——per-turn 在
            # _prepare_turn（loop.py）注入 svc["memory"] = runtime.memory_view（scoped）。
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

    async def _deliver_tool_request(
        self, target_connection_id: str | None, frame: dict,
    ) -> None:
        """Desktop 工具请求定向投递（bridge._deliver 回调，spec §5.2）。

        bridge 已构造 `ToolRequestMessage(...).wire_dict()`（camelCase 帧），此处只负责
        定位目标连接：遍历 `agent.output.channels`（公开只读快照），只匹配
        `client_platform == "electron" && connection_id == target_connection_id` 的
        WebSocketChannel → `send_json(frame)`（定向单连接，非广播）。找不到精确目标
        时抛错，让 bridge 立即收敛为 DESKTOP_UNAVAILABLE，而不是静默丢帧后等超时。

        Args:
            target_connection_id: bridge 在 call doc 中登记的目标 Electron 连接 ID。
            frame: tool.request wire 帧（camelCase dict）。

        Raises:
            RuntimeError: 目标连接为空或已经不在输出通道中。
        """
        if not target_connection_id:
            raise RuntimeError("DesktopToolBridge 投递缺少目标连接")
        target = None
        for ch in self._output.channels:
            if (getattr(ch, "client_platform", "") == "electron"
                    and getattr(ch, "connection_id", None) == target_connection_id):
                target = ch
                break
        if target is None:
            raise RuntimeError(
                f"DesktopToolBridge 目标连接不可用: {target_connection_id}")
        # send_json 仅 WebSocketChannel 定义（OutputChannel ABC 无此方法），
        # 已按 electron + connection_id 精确过滤，运行时必为 WebSocketChannel。
        # `raise_on_error=True`（S2-H6）：send_json 的默认语义是**广播**用的
        # "吞异常 + warning"，而本路径是**定向投递**——投递失败必须立刻抛，
        # 否则 bridge 只能等满 timeout，把"桌面不可用"记成"桌面超时"，
        # 它自己的快速回退分支（DESKTOP_UNAVAILABLE）成为死代码。
        await cast(WebSocketChannel, target).send_json(frame, raise_on_error=True)

    async def _start_worker_pool(self) -> None:
        """启动 Worker Pool + embedding 预加载后台任务。"""
        await self._scheduler.start()
        self._embed_task = self._lifecycle_tasks.spawn(
            self._long_term.embedding_provider.ensure(), name="embedding-preload")

    async def _start_idle_and_events(self) -> None:
        """启动 IdleMonitor + 主事件循环（独立 asyncio.Task）。

        ``_process_events`` 经 AsyncTaskScope 调度——此前裸 create_task 无 done
        callback，分发链上任何异常都会**静默杀死 EventBus 消费者**，此后 Agent
        再也收不到用户输入，且直到关机才由 stop() 的 await 把异常翻出来。
        现在由 scope 的观察回调即时记 ERROR（ARCH-INC-P2-03）。
        """
        await self._idle.start()
        self._event_task = self._lifecycle_tasks.spawn(
            self._process_events(), name="agent-events")

    async def _stop_event_task(self) -> None:
        """停止 _process_events 事件循环——防止它在 shutdown 期间继续 post 新任务。"""
        logger.debug("Cancelling event task...")
        await cancel_and_drain(
            self._event_task, timeout=3.0, label="agent-event")
        self._event_task = None

    def _stop_cognitive_and_tts(self) -> None:
        """Cancel 当前 cognitive task + TTS 任务（belt-and-suspenders）。"""
        self._cognitive.mark_shutting_down()  # 门禁——after_response 检查后提前退出
        self._cognitive.cancel_current()
        self._cognitive.tts_manager.cancel_all()

    async def _stop_embedding_if_running(self) -> None:
        """取消并等待 embedding 预加载任务完成。

        注意：``ensure(wait=False)`` 只是**外层**任务，真正的加载任务由
        EmbeddingProvider 自己持有（``embedding_provider.py`` 的 ``self._task``），
        由 ``LongTermMemory.close()`` 负责收尾——这里取消外层不影响它。
        """
        await cancel_and_drain(
            self._embed_task, timeout=3.0, label="agent-embed-preload")
        self._embed_task = None

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
