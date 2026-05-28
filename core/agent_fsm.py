"""Agent finite state machine.

States: IDLE → THINKING → EXECUTING → THINKING → SPEAKING → IDLE
INTERRUPTED can be entered from any state (except IDLE).
"""

import logging

from .schema import AgentState, VALID_TRANSITIONS  # noqa: F401 — re-export

logger = logging.getLogger(__name__)


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
