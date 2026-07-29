"""Agent 运行时状态接口和 FSM-backed 实现。

外部消费者通过 agent.status 查询状态，不感知 AgentFSM。
CognitiveLoop 通过 agent.phases 驱动状态转换。

接口设计遵循：属性 = 即刻可得的事实（property），方法 = 可能有副作用。
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from .agent_fsm import AgentFSM, AgentState

logger = logging.getLogger(__name__)


# ── 对外只读接口 ──────────────────────────────────────────────────────────────

class AgentStatus(ABC):
    """Agent 运行时状态的语义接口——面向外部消费者。

    不暴露 FSM 状态名。消费者通过语义属性查询，不感知底层实现。
    """

    # ── 语义查询（@property）──

    @property
    @abstractmethod
    def is_accepting_input(self) -> bool:
        """可接受新用户消息？（IDLE 或 IDLE_BACKGROUND_TASK）"""
        ...

    @property
    @abstractmethod
    def is_completely_idle(self) -> bool:
        """完全空闲，无任何进行中工作？（仅 IDLE）"""
        ...

    @property
    @abstractmethod
    def is_speaking(self) -> bool:
        """正在输出语音/文本给用户？（SPEAKING）"""
        ...

    @property
    @abstractmethod
    def should_suppress_vad_interrupt(self) -> bool:
        """应抑制 VAD 语音检测中断？（THINKING 或 EXECUTING）"""
        ...

    # ── 诊断 ──

    @abstractmethod
    def current_name(self) -> str:
        """当前状态名，仅供日志和调试仪表板使用。"""
        ...

    @abstractmethod
    def previous_name(self) -> str | None:
        """上一状态名，仅供调试仪表板使用。"""
        ...

    # ── 事件订阅 ──

    @abstractmethod
    def on_change(
        self, callback: Callable[[str], Awaitable[None]],
    ) -> None:
        """注册状态变更回调。回调接收语义状态名（如 "speaking"）。

        每次 transition 后调用，不做 edge detection——
        如果连续两次进入同一状态，每次都触发。
        """
        ...

    @abstractmethod
    async def wait_until_idle(
        self,
        *,
        include_background: bool = False,
        timeout: float | None = None,
    ) -> None:
        """阻塞直到进入空闲状态。

        include_background=True: IDLE 或 IDLE_BACKGROUND_TASK 都算。
        include_background=False: 仅 IDLE。
        超时不抛异常——调用方自行检查状态。
        """
        ...

    # ── 内部 ──

    async def _notify(self, state_name: str) -> None:
        """内部：通知所有 on_change 回调。由 FSMBackedPhaseController 在每个
        transition 后调用。单个回调异常不影响其他回调。"""
        for cb in getattr(self, '_listeners', []):
            try:
                await cb(state_name)
            except Exception:
                logger.warning(
                    "AgentStatus 回调异常: %s", state_name, exc_info=True,
                )


# ── FSM-backed 实现 ───────────────────────────────────────────────────────────

class FSMBackedStatus(AgentStatus):
    """基于 AgentFSM 的 AgentStatus 实现。"""

    def __init__(self, fsm: AgentFSM) -> None:
        self._fsm = fsm
        self._listeners: list[Callable[[str], Awaitable[None]]] = []

    # ── 语义查询 ──

    @property
    def is_accepting_input(self) -> bool:
        return self._fsm.state in (AgentState.IDLE, AgentState.IDLE_BACKGROUND_TASK)

    @property
    def is_completely_idle(self) -> bool:
        return self._fsm.state == AgentState.IDLE

    @property
    def is_speaking(self) -> bool:
        return self._fsm.state == AgentState.SPEAKING

    @property
    def should_suppress_vad_interrupt(self) -> bool:
        return self._fsm.state in (AgentState.THINKING, AgentState.EXECUTING)

    def current_name(self) -> str:
        return str(self._fsm.state)

    def previous_name(self) -> str | None:
        return str(self._fsm.previous) if self._fsm.previous else None

    def on_change(
        self, callback: Callable[[str], Awaitable[None]],
    ) -> None:
        self._listeners.append(callback)

    async def wait_until_idle(
        self,
        *,
        include_background: bool = False,
        timeout: float | None = None,
    ) -> None:
        # 当前已满足条件？立即返回
        if self.is_completely_idle:
            return
        if include_background and self._fsm.state == AgentState.IDLE_BACKGROUND_TASK:
            return

        event = asyncio.Event()

        async def _cb(state_name: str) -> None:
            if state_name == "idle":
                event.set()
            elif include_background and state_name == "idle_background_task":
                event.set()

        self.on_change(_cb)
        try:
            if timeout is not None:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            else:
                await event.wait()
        except asyncio.TimeoutError:
            pass
        finally:
            self._listeners.remove(_cb)


# ── 对内写入接口 ──────────────────────────────────────────────────────────────

class CognitivePhaseController(ABC):
    """认知管线阶段控制器——CognitiveLoop 通过它驱动状态转换。

    封装 FSM transition + StateChangeData 发布 + 输出指示器 + TTS 同步。
    外部消费者不应访问此接口——它是 CognitiveLoop 的实现工具。
    """

    @abstractmethod
    async def enter_thinking(self) -> None:
        """进入 THINKING 阶段。封装 transition + post + _notify。"""
        ...

    @abstractmethod
    async def enter_executing(self) -> None:
        """进入 EXECUTING 阶段。封装 output.send("") 切断气泡
        + output.typing(False) + transition + post + _notify。"""
        ...

    @abstractmethod
    async def return_to_thinking(self) -> None:
        """从 EXECUTING 返回 THINKING。封装 output.typing(True)
        + transition + post + _notify。"""
        ...

    @abstractmethod
    async def enter_speaking(self) -> None:
        """进入 SPEAKING 阶段（首句输出时）。封装 output.typing(False)
        + transition + post + _notify。"""
        ...

    @abstractmethod
    async def finish_turn(self, *, has_background_tasks: bool) -> None:
        """结束当前 turn，转换到 IDLE 或 IDLE_BACKGROUND_TASK。

        封装 TTS wait_all()、defensive SPEAKING 过渡、
        transition + post + _notify。
        """
        ...

    @abstractmethod
    async def handle_interrupt(self) -> None:
        """中断状态转换——进入 INTERRUPTED，50ms 后回到 IDLE。

        封装 transition(→ INTERRUPTED) + post + _notify
        + asyncio.sleep(0.05) + transition(→ IDLE) + post + _notify。
        资源清理（reset/cancel/clear）由调用方完成——这些是流式管线的职责。
        """
        ...


# ── FSM-backed PhaseController ────────────────────────────────────────────────

class FSMBackedPhaseController(CognitivePhaseController):
    """基于 AgentFSM 的 CognitivePhaseController 实现。"""

    def __init__(
        self,
        fsm: AgentFSM,
        bus: "EventBus",
        output: "OutputManager",
        tts_mgr: "TTSStreamManager | None",
        status: FSMBackedStatus,
    ) -> None:
        self._fsm = fsm
        self._bus = bus
        self._output = output
        self._tts_mgr = tts_mgr
        self._status = status

    # ── phase 入口 ──

    async def enter_thinking(self) -> None:
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import StateChangeData
        from core.utils import get_request_id

        self._fsm.transition(AgentState.THINKING)
        self._bus.post(
            StateChangeData(state="thinking", request_id=get_request_id()),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("thinking")

    async def enter_executing(self) -> None:
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import StateChangeData
        from core.utils import get_request_id

        await self._output.send("")
        await self._output.typing(False)
        self._fsm.transition(AgentState.EXECUTING)
        self._bus.post(
            StateChangeData(state="executing", request_id=get_request_id()),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("executing")

    async def return_to_thinking(self) -> None:
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import StateChangeData
        from core.utils import get_request_id

        self._fsm.transition(AgentState.THINKING)
        self._bus.post(
            StateChangeData(state="thinking", request_id=get_request_id()),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._output.typing(True)
        await self._status._notify("thinking")

    async def enter_speaking(self) -> None:
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import StateChangeData
        from core.utils import get_request_id

        await self._output.typing(False)
        self._fsm.transition(AgentState.SPEAKING)
        self._bus.post(
            StateChangeData(state="speaking", request_id=get_request_id()),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("speaking")

    async def finish_turn(self, *, has_background_tasks: bool) -> None:
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import StateChangeData
        from core.utils import get_request_id

        if self._tts_mgr is not None:
            await self._tts_mgr.wait_all()

        if has_background_tasks:
            if self._fsm.state == AgentState.SPEAKING:
                self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
            else:
                if self._fsm.can_transition(AgentState.SPEAKING):
                    self._fsm.transition(AgentState.SPEAKING)
                self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
            self._bus.post(
                StateChangeData(
                    state="idle_background_task", request_id=get_request_id(),
                ),
                priority=EventPriority.STATE_CHANGE,
            )
            await self._status._notify("idle_background_task")
        else:
            if self._fsm.state == AgentState.THINKING:
                self._fsm.transition(AgentState.SPEAKING)
            self._fsm.transition(AgentState.IDLE)
            self._bus.post(
                StateChangeData(state="idle", request_id=get_request_id()),
                priority=EventPriority.STATE_CHANGE,
            )
            await self._status._notify("idle")

    async def handle_interrupt(self) -> None:
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import StateChangeData
        from core.utils import get_request_id

        self._fsm.transition(AgentState.INTERRUPTED)
        self._bus.post(
            StateChangeData(state="interrupted", request_id=get_request_id()),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("interrupted")
        await asyncio.sleep(0.05)
        self._fsm.transition(AgentState.IDLE)
        self._bus.post(
            StateChangeData(state="idle", request_id=get_request_id()),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("idle")
