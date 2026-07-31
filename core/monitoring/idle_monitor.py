"""IdleMonitor — wake-word detection + proactive speaking triggers.

Runs in IDLE state. Simulates wake-word via keyboard (press Enter to "wake").
Proactive mode triggers a lightweight "should I speak?" check on a timer.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from .event_bus import EventBus, EventPriority, EventType
from core.status.agent_fsm import AgentFSM
from ..config.character_config import CharacterConfig
from core.schema.event import SystemTickData

if TYPE_CHECKING:
    from core.status import AgentStatus

logger = logging.getLogger(__name__)


class IdleMonitor:
    """Monitors IDLE state for wake and proactive triggers."""

    def __init__(
        self,
        character: CharacterConfig,
        fsm: AgentFSM,
        event_bus: EventBus,
        status: "AgentStatus | None" = None,
    ):
        """初始化空闲监听器。

        Args:
            character: 角色配置，包含唤醒词和主动触发配置。
            fsm: Agent 状态机，用于检查当前状态。
            event_bus: 事件总线，用于投递主动触发事件。
            status: Agent 状态查询接口（可选，回退到 FSM 内联检查）。
        """
        self._character = character
        self._fsm = fsm
        self._bus = event_bus
        if status is None:
            from core.status import FSMBackedStatus
            self.status = FSMBackedStatus(fsm)
        else:
            self.status = status
        self._last_interaction: float = time.monotonic()
        self._last_time_of_day_trigger: str = ""  # 防重：上次触发的 "YYYY-MM-DD HH:MM"（含日期，跨天不抑制）
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the idle monitor loop."""
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("IdleMonitor started (wake_words=%s, proactive=%s)",
                     self._character.wake_words,
                     self._character.proactive.enabled)

    async def stop(self) -> None:
        """停止空闲监听循环，取消后台任务。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def touch(self) -> None:
        """Mark user interaction — resets silence timer."""
        self._last_interaction = time.monotonic()

    async def _loop(self) -> None:
        """空闲监听主循环，每秒检查一次主动触发条件。"""
        proactive_interval = self._character.proactive.interval

        while self._running:
            try:
                await asyncio.sleep(1.0)  # Tick every second

                if not self.status.is_completely_idle:
                    continue

                # Check proactive triggers
                if self._character.proactive.enabled:
                    elapsed = time.monotonic() - self._last_interaction
                    if elapsed >= proactive_interval:
                        await self._check_proactive()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("IdleMonitor loop error", exc_info=True)

    async def _check_proactive(self) -> None:
        """Evaluate whether to speak proactively."""
        triggers = self._character.proactive.triggers
        if not triggers:
            return

        for trigger in triggers:
            ttype = trigger.get("type", "")
            if ttype == "silence_timeout":
                duration = trigger.get("duration", 600)
                elapsed = time.monotonic() - self._last_interaction
                if elapsed >= duration:
                    self._bus.post(
                        SystemTickData(reason="silence_timeout", elapsed=elapsed),
                        priority=EventPriority.NORMAL,
                    )
                    logger.info("Proactive trigger: silence_timeout (elapsed=%ds)", int(elapsed))
                    self._last_interaction = time.monotonic()
                    return

            elif ttype == "time_of_day":
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                # 防重：同一分钟只触发一次，避免每秒重复投递。
                # 键含日期——否则跨天同刻会被误判为已触发，每日定时主动搭话仅触发一次
                if now == self._last_time_of_day_trigger:
                    return
                times = trigger.get("times", [])
                if now[11:] in times:
                    self._last_time_of_day_trigger = now
                    self._bus.post(
                        SystemTickData(reason="time_of_day", time=now),
                        priority=EventPriority.NORMAL,
                    )
                    logger.info("Proactive trigger: time_of_day (%s)", now[11:])
                    return
