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


@pytest.mark.unit
class TestFSMBackedPhaseController:
    """FSMBackedPhaseController 的阶段转换测试。"""

    @pytest.fixture
    def fsm(self) -> AgentFSM:
        return AgentFSM()

    @pytest.fixture
    def bus(self) -> Mock:
        return Mock()

    @pytest.fixture
    def output(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def status(self, fsm) -> "FSMBackedStatus":
        from core.agent.status import FSMBackedStatus
        return FSMBackedStatus(fsm)

    def _make_controller(self, fsm, bus, output, status, tts_mgr=None):
        from core.agent.status import FSMBackedPhaseController
        return FSMBackedPhaseController(
            fsm=fsm, bus=bus, output=output,
            tts_mgr=tts_mgr, status=status,
        )

    @pytest.mark.asyncio
    async def test_enter_thinking(self, fsm, bus, output, status):
        from core.agent.status import FSMBackedPhaseController
        from core.monitoring.event_bus import EventPriority
        ph = FSMBackedPhaseController(
            fsm=fsm, bus=bus, output=output, tts_mgr=None, status=status,
        )
        await ph.enter_thinking()
        assert fsm.state == AgentState.THINKING
        bus.post.assert_called_once()
        call_args = bus.post.call_args
        assert call_args[0][0].state == "thinking"
        assert call_args[1]["priority"] == EventPriority.STATE_CHANGE

    @pytest.mark.asyncio
    async def test_enter_executing(self, fsm, bus, output, status):
        fsm.transition(AgentState.THINKING)
        ph = self._make_controller(fsm, bus, output, status)
        await ph.enter_executing()
        assert fsm.state == AgentState.EXECUTING
        output.send.assert_called_once_with("")
        output.typing.assert_called_once_with(False)
        bus.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_return_to_thinking(self, fsm, bus, output, status):
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.EXECUTING)
        ph = self._make_controller(fsm, bus, output, status)
        await ph.return_to_thinking()
        assert fsm.state == AgentState.THINKING
        output.typing.assert_called_once_with(True)
        bus.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_enter_speaking(self, fsm, bus, output, status):
        fsm.transition(AgentState.THINKING)
        ph = self._make_controller(fsm, bus, output, status)
        await ph.enter_speaking()
        assert fsm.state == AgentState.SPEAKING
        output.typing.assert_called_once_with(False)
        bus.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_finish_turn_idle(self, fsm, bus, output, status):
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        ph = self._make_controller(fsm, bus, output, status)
        await ph.finish_turn(has_background_tasks=False)
        assert fsm.state == AgentState.IDLE
        bus.post.assert_called_once()
        assert bus.post.call_args[0][0].state == "idle"

    @pytest.mark.asyncio
    async def test_finish_turn_idle_background(self, fsm, bus, output, status):
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        ph = self._make_controller(fsm, bus, output, status)
        await ph.finish_turn(has_background_tasks=True)
        assert fsm.state == AgentState.IDLE_BACKGROUND_TASK
        bus.post.assert_called_once()
        assert bus.post.call_args[0][0].state == "idle_background_task"

    @pytest.mark.asyncio
    async def test_finish_turn_from_thinking(self, fsm, bus, output, status):
        """finish_turn 从 THINKING 出发——defensive SPEAKING 过渡。"""
        fsm.transition(AgentState.THINKING)
        # 仍在 THINKING——模拟纯工具调用轮次（无文本输出）
        ph = self._make_controller(fsm, bus, output, status)
        await ph.finish_turn(has_background_tasks=False)
        # 应经过 SPEAKING 再到 IDLE
        assert fsm.state == AgentState.IDLE
        bus.post.assert_called_once()
        assert bus.post.call_args[0][0].state == "idle"

    @pytest.mark.asyncio
    async def test_finish_turn_waits_tts(self, fsm, bus, output, status):
        """finish_turn 等 TTS 完成再 transition。"""
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        tts_mgr = AsyncMock()
        tts_mgr.wait_all = AsyncMock()
        ph = self._make_controller(fsm, bus, output, status, tts_mgr=tts_mgr)
        await ph.finish_turn(has_background_tasks=False)
        tts_mgr.wait_all.assert_awaited_once()
        assert fsm.state == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_handle_interrupt(self, fsm, bus, output, status):
        fsm.transition(AgentState.THINKING)
        ph = self._make_controller(fsm, bus, output, status)
        await ph.handle_interrupt()
        # 最终回到 IDLE
        assert fsm.state == AgentState.IDLE
        # 两次 post：interrupted + idle
        assert bus.post.call_count == 2
        first_state = bus.post.call_args_list[0][0][0].state
        second_state = bus.post.call_args_list[1][0][0].state
        assert first_state == "interrupted"
        assert second_state == "idle"

    @pytest.mark.asyncio
    async def test_tts_none_does_not_crash(self, fsm, bus, output, status):
        """TTS 为 None 时 finish_turn 不崩溃。"""
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        ph = self._make_controller(fsm, bus, output, status, tts_mgr=None)
        await ph.finish_turn(has_background_tasks=False)
        assert fsm.state == AgentState.IDLE
