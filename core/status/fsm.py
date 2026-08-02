"""FSM-backed AgentStatus + CognitivePhaseController 实现。

依赖 AgentFSM（core.status.agent_fsm），将语义接口映射到具体状态转换。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from core.status.agent_fsm import AgentFSM, AgentState
from core.status.protocol import AgentStatus, CognitivePhaseController

if TYPE_CHECKING:
    from core.adapter.tts_adapter import TTSStreamManager
    from core.monitoring.event_bus import EventBus
    from Module.output.manager import OutputManager

logger = logging.getLogger(__name__)


# ── FSM-backed Status ─────────────────────────────────────────────────────────

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
            # wait_all 后 flush emoji——此时最后一句 TTS 已完成，
            # 残留 emoji 不会提前广播，依附顺序（"句子😊"）保持正确
            await self._tts_mgr.flush_emojis()

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
