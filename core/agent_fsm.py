"""Agent finite state machine.

States: IDLE → THINKING → EXECUTING → THINKING → SPEAKING → IDLE
INTERRUPTED can be entered from any state (except IDLE).
"""

from enum import StrEnum, auto
import logging

logger = logging.getLogger(__name__)


class AgentState(StrEnum):
    IDLE = auto()
    THINKING = auto()
    EXECUTING = auto()
    SPEAKING = auto()
    INTERRUPTED = auto()


VALID_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.IDLE:        {AgentState.THINKING},
    AgentState.THINKING:    {AgentState.EXECUTING, AgentState.SPEAKING, AgentState.INTERRUPTED},
    AgentState.EXECUTING:   {AgentState.THINKING, AgentState.INTERRUPTED},
    # SPEAKING can go to EXECUTING or THINKING when tool_calls arrive mid-stream
    AgentState.SPEAKING:    {AgentState.IDLE, AgentState.EXECUTING, AgentState.THINKING, AgentState.INTERRUPTED},
    AgentState.INTERRUPTED: {AgentState.IDLE},
}


class AgentFSM:
    """Finite state machine for the Agent cognitive pipeline."""

    def __init__(self):
        self._state = AgentState.IDLE
        self._previous: AgentState | None = None

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def previous(self) -> AgentState | None:
        return self._previous

    def can_transition(self, to_state: AgentState) -> bool:
        return to_state in VALID_TRANSITIONS.get(self._state, set())

    def transition(self, to_state: AgentState) -> None:
        if not self.can_transition(to_state):
            raise ValueError(f"Invalid transition: {self._state} → {to_state}")
        self._previous = self._state
        self._state = to_state
        logger.debug("FSM: %s → %s", self._previous, self._state)

    def interrupt(self) -> bool:
        """Attempt interrupt from current state. Returns True if interrupt was valid."""
        if self._state == AgentState.IDLE:
            return False
        if self.can_transition(AgentState.INTERRUPTED):
            self.transition(AgentState.INTERRUPTED)
            return True
        return False
