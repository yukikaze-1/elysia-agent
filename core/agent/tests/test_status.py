"""AgentStatus 和 CognitivePhaseController 的单元测试。"""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from core.agent.agent_fsm import AgentFSM, AgentState


@pytest.mark.unit
class TestFSMBackedStatus:
    """FSMBackedStatus 的语义属性测试。"""

    @pytest.fixture
    def fsm(self) -> AgentFSM:
        return AgentFSM()

    def test_is_accepting_input_idle(self, fsm):
        from core.agent.status import FSMBackedStatus
        status = FSMBackedStatus(fsm)
        assert status.is_accepting_input is True       # IDLE 初始状态
        assert status.is_completely_idle is True

    def test_is_accepting_input_thinking(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)
        assert status.is_accepting_input is False
        assert status.is_completely_idle is False

    def test_is_accepting_input_idle_background(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
        status = FSMBackedStatus(fsm)
        assert status.is_accepting_input is True        # IDLE_BG 算接受输入
        assert status.is_completely_idle is False       # 但不算完全空闲

    def test_is_speaking(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        status = FSMBackedStatus(fsm)
        assert status.is_speaking is True

    def test_is_speaking_false(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)
        assert status.is_speaking is False

    def test_should_suppress_vad_thinking(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)
        assert status.should_suppress_vad_interrupt is True

    def test_should_suppress_vad_executing(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.EXECUTING)
        status = FSMBackedStatus(fsm)
        assert status.should_suppress_vad_interrupt is True

    def test_should_suppress_vad_speaking(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        status = FSMBackedStatus(fsm)
        assert status.should_suppress_vad_interrupt is False   # SPEAKING 允许打断

    def test_current_name(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)
        assert status.current_name() == "thinking"

    def test_previous_name(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        status = FSMBackedStatus(fsm)
        assert status.previous_name() == "thinking"

    def test_previous_name_none(self, fsm):
        from core.agent.status import FSMBackedStatus
        status = FSMBackedStatus(fsm)
        assert status.previous_name() is None

    @pytest.mark.asyncio
    async def test_on_change_notify(self, fsm):
        from core.agent.status import FSMBackedStatus
        status = FSMBackedStatus(fsm)
        calls = []

        async def cb(name: str) -> None:
            calls.append(name)

        status.on_change(cb)
        await status._notify("thinking")
        assert calls == ["thinking"]

    @pytest.mark.asyncio
    async def test_on_change_exception_isolated(self, fsm):
        from core.agent.status import FSMBackedStatus
        status = FSMBackedStatus(fsm)
        calls = []

        async def bad_cb(name: str) -> None:
            raise RuntimeError("boom")

        async def good_cb(name: str) -> None:
            calls.append(name)

        status.on_change(bad_cb)
        status.on_change(good_cb)
        await status._notify("speaking")
        assert calls == ["speaking"]  # good_cb 仍然被调用

    @pytest.mark.asyncio
    async def test_wait_until_idle_already_idle(self, fsm):
        from core.agent.status import FSMBackedStatus
        status = FSMBackedStatus(fsm)
        # 当前已是 IDLE，应立即返回
        await status.wait_until_idle(timeout=0.05)
        # 不超时不抛异常——通过

    @pytest.mark.asyncio
    async def test_wait_until_idle_waits(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)

        async def notify_later() -> None:
            await asyncio.sleep(0.02)
            await status._notify("idle")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(notify_later())
            tg.create_task(status.wait_until_idle(timeout=0.5))
        # 不超时——通过

    @pytest.mark.asyncio
    async def test_wait_until_idle_background(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)

        async def notify_later() -> None:
            await asyncio.sleep(0.02)
            await status._notify("idle_background_task")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(notify_later())
            tg.create_task(status.wait_until_idle(include_background=True, timeout=0.5))
        # 不超时——通过

    @pytest.mark.asyncio
    async def test_wait_until_idle_timeout(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)
        await status.wait_until_idle(timeout=0.02)
        # timeout 不抛异常——通过
