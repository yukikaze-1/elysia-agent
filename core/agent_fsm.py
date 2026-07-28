"""Agent finite state machine.

States: IDLE → THINKING → EXECUTING → THINKING → SPEAKING → IDLE
        SPEAKING → IDLE_BACKGROUND_TASK
        IDLE_BACKGROUND_TASK → THINKING / INTERRUPTED
INTERRUPTED can be entered from any state (except IDLE).
"""

import logging

from .schema import AgentState, VALID_TRANSITIONS  # noqa: F401 — re-export

logger = logging.getLogger(__name__)


class AgentFSM:
    """Finite state machine for the Agent cognitive pipeline."""

    def __init__(self):
        """初始化状态机，默认进入 IDLE 状态。"""
        self._state = AgentState.IDLE
        self._previous: AgentState | None = None

    @property
    def state(self) -> AgentState:
        """当前状态。

        Returns:
            当前的 AgentState 枚举值。
        """
        return self._state

    @property
    def previous(self) -> AgentState | None:
        """上一个状态。

        Returns:
            上一个 AgentState 枚举值，初始为 None。
        """
        return self._previous

    def can_transition(self, to_state: AgentState) -> bool:
        """检查从当前状态是否能转换到目标状态。

        Args:
            to_state: 目标状态。

        Returns:
            True 表示允许该状态转换。
        """
        return to_state in VALID_TRANSITIONS.get(self._state, set())

    def transition(self, to_state: AgentState) -> None:
        """执行状态转换。

        Args:
            to_state: 目标状态。同状态转换是幂等的，不会抛异常。

        Raises:
            ValueError: 当转换不在 VALID_TRANSITIONS 中时抛出。
        """
        if to_state == self._state:
            return  # 同状态幂等，避免 idle→idle 抛异常
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

    def reset(self) -> None:
        """Force reset to IDLE. Used on service restart to clear any stuck state."""
        self._previous = self._state
        self._state = AgentState.IDLE
        logger.debug("FSM: reset %s → IDLE", self._previous)
