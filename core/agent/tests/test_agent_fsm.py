# core/agent/tests/test_agent_fsm.py
"""AgentFSM 状态机单元测试。"""

import pytest
from core.agent.agent_fsm import AgentFSM
from core.schema import AgentState

pytestmark = pytest.mark.unit


class TestAgentFSM:
    """AgentFSM 纯单元测试——无外部依赖"""

    def test_initial_state_is_idle(self):
        fsm = AgentFSM()
        assert fsm.state == AgentState.IDLE

    def test_initial_previous_is_none(self):
        fsm = AgentFSM()
        assert fsm.previous is None

    def test_idle_to_thinking(self):
        fsm = AgentFSM()
        assert fsm.can_transition(AgentState.THINKING)
        fsm.transition(AgentState.THINKING)
        assert fsm.state == AgentState.THINKING
        assert fsm.previous == AgentState.IDLE

    def test_thinking_to_speaking(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        assert fsm.can_transition(AgentState.SPEAKING)
        fsm.transition(AgentState.SPEAKING)
        assert fsm.state == AgentState.SPEAKING

    def test_speaking_to_idle(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        assert fsm.can_transition(AgentState.IDLE)
        fsm.transition(AgentState.IDLE)
        assert fsm.state == AgentState.IDLE

    def test_invalid_transition_raises_value_error(self):
        """非法状态转换抛 ValueError——IDLE 不能直接到 SPEAKING"""
        fsm = AgentFSM()
        with pytest.raises(ValueError):
            fsm.transition(AgentState.SPEAKING)

    def test_same_state_transition_is_idempotent(self):
        """同状态转换幂等——IDLE→IDLE 不抛异常"""
        fsm = AgentFSM()
        fsm.transition(AgentState.IDLE)  # 不应抛异常
        assert fsm.state == AgentState.IDLE

    def test_reset_returns_to_idle(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        fsm.reset()
        assert fsm.state == AgentState.IDLE
        assert fsm.previous == AgentState.THINKING

    def test_interrupt_from_thinking(self):
        """INTERRUPTED 可从 THINKING 进入"""
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        assert fsm.interrupt()
        assert fsm.state == AgentState.INTERRUPTED

    def test_interrupt_from_idle_returns_false(self):
        """IDLE 状态不可 interrupt，返回 False"""
        fsm = AgentFSM()
        assert not fsm.interrupt()
        assert fsm.state == AgentState.IDLE
