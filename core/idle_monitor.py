"""IdleMonitor — wake-word detection + proactive speaking triggers.

Runs in IDLE state. Simulates wake-word via keyboard (press Enter to "wake").
Proactive mode triggers a lightweight "should I speak?" check on a timer.
"""

import asyncio
import logging
import time
from datetime import datetime

from .event_bus import EventBus, EventPriority, EventType
from .agent_fsm import AgentFSM, AgentState
from .config import CharacterConfig
from core.schema.event import SystemTickData

logger = logging.getLogger(__name__)


class IdleMonitor:
    """Monitors IDLE state for wake and proactive triggers."""

    def __init__(
        self,
        character: CharacterConfig,
        fsm: AgentFSM,
        event_bus: EventBus,
    ):
        self._character = character
        self._fsm = fsm
        self._bus = event_bus
        self._last_interaction: float = time.monotonic()
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
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def touch(self) -> None:
        """Mark user interaction — resets silence timer."""
        self._last_interaction = time.monotonic()

    async def _loop(self) -> None:
        proactive_interval = self._character.proactive.interval

        while self._running:
            try:
                await asyncio.sleep(1.0)  # Tick every second

                if self._fsm.state != AgentState.IDLE:
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
                now = datetime.now().strftime("%H:%M")
                times = trigger.get("times", [])
                if now in times:
                    self._bus.post(
                        SystemTickData(reason="time_of_day", time=now),
                        priority=EventPriority.NORMAL,
                    )
                    logger.info("Proactive trigger: time_of_day (%s)", now)
                    return
