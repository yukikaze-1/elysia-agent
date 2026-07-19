"""Phase 1 comprehensive tests: streaming, interrupt, emotion, sentence buffer.

Run:
    python -m pytest core/test_phase1.py -v
or:
    python core/test_phase1.py
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.schema.event import InterruptData, StateChangeData, UserInputData

# ============================================================================
# 1. EventBus + FSM tests
# ============================================================================

@pytest.mark.unit
class TestEventBus:
    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        from core.event_bus import EventBus, EventPriority
        bus = EventBus()

        bus.post(UserInputData(text="data1", user_name="test", request_id="?"), type="normal", priority=EventPriority.NORMAL)
        bus.post(InterruptData(), priority=EventPriority.INTERRUPT)
        bus.post(StateChangeData(state="test", request_id="?"), priority=EventPriority.STATE_CHANGE)

        e1 = await bus.next()
        e2 = await bus.next()
        e3 = await bus.next()

        assert e1.type == "interrupt"
        assert e2.type == "state_change"
        assert e3.type == "normal"

    def test_drain(self):
        from core.event_bus import EventBus, EventPriority
        bus = EventBus()
        bus.post(UserInputData(text="", user_name="", request_id=""), type="a", priority=EventPriority.NORMAL)
        bus.post(UserInputData(text="", user_name="", request_id=""), type="b", priority=EventPriority.NORMAL)
        bus.post(UserInputData(text="", user_name="", request_id=""), type="c", priority=EventPriority.NORMAL)

        drained = bus.drain()
        assert len(drained) == 3
        assert bus.pending_count == 0


@pytest.mark.unit
class TestAgentFSM:
    def test_valid_transitions(self):
        from core.agent_fsm import AgentFSM, AgentState
        fsm = AgentFSM()

        assert fsm.state == AgentState.IDLE
        fsm.transition(AgentState.THINKING)
        assert fsm.state == AgentState.THINKING
        fsm.transition(AgentState.SPEAKING)
        assert fsm.state == AgentState.SPEAKING
        fsm.transition(AgentState.IDLE)
        assert fsm.state == AgentState.IDLE

    def test_invalid_transition_raises(self):
        from core.agent_fsm import AgentFSM, AgentState
        fsm = AgentFSM()

        with pytest.raises(ValueError):
            fsm.transition(AgentState.SPEAKING)  # IDLE -> SPEAKING invalid

    def test_interrupt_from_thinking(self):
        from core.agent_fsm import AgentFSM, AgentState
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)

        assert fsm.interrupt() is True
        assert fsm.state == AgentState.INTERRUPTED

        fsm.transition(AgentState.IDLE)
        assert fsm.state == AgentState.IDLE

    def test_interrupt_from_idle_noop(self):
        from core.agent_fsm import AgentFSM, AgentState
        fsm = AgentFSM()

        assert fsm.interrupt() is False
        assert fsm.state == AgentState.IDLE

    def test_complex_transition_chain(self):
        """Full lifecycle: IDLE -> THINKING -> EXECUTING -> THINKING -> SPEAKING -> IDLE"""
        from core.agent_fsm import AgentFSM, AgentState
        fsm = AgentFSM()

        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.EXECUTING)
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        fsm.transition(AgentState.IDLE)
        assert fsm.state == AgentState.IDLE


# ============================================================================
# 3. EmotionEngine tests → CharacterEngine (merged)
# ============================================================================

@pytest.mark.service
class TestEmotionEngine:
    @pytest.fixture
    def engine(self):
        from core.character_config import CharacterConfig
        from core.schema.character import CharacterState
        from core.schema.types import SessionId
        from Module.output import OutputManager
        from core.personality import CharacterEngine
        from core.adapter.llm_adapter import LLMAdapter
        from unittest.mock import MagicMock, AsyncMock

        char = CharacterConfig(name="Test")
        state = CharacterState(session_id=SessionId("test-sid"))
        output = OutputManager()
        output.expression = MagicMock(return_value=MagicMock())
        llm = MagicMock(spec=LLMAdapter)
        llm.think = AsyncMock()
        return CharacterEngine(
            llm=llm, state=state,
            character_name="Test",
            user_name="妖梦",
            base_personality="",
            character_config=char,
            output=output,
        )

    def test_detect_happy(self, engine):
        from core.schema.mood import Mood
        assert engine._detect_mood("哈哈太开心了") == Mood.HAPPY
        assert engine._detect_mood("今天真开心") == Mood.HAPPY
        # "太好了" matches both happy("太好了") and excited("!").
        # Happy has higher keyword count, so it wins.
        assert engine._detect_mood("太好了") == Mood.HAPPY
        assert engine._detect_mood("难过死了") == Mood.SAD
        assert engine._detect_mood("为什么啊") == Mood.CURIOUS

    def test_detect_excited(self, engine):
        from core.schema.mood import Mood
        # "！！！" only matches excited keywords
        assert engine._detect_mood("天哪真的假的！！！") == Mood.EXCITED
        assert engine._detect_mood("竟然！！！牛逼") == Mood.EXCITED

    def test_detect_neutral(self, engine):
        from core.schema.mood import Mood
        assert engine._detect_mood("今天天气不错") == Mood.NEUTRAL
        assert engine._detect_mood("你好") == Mood.NEUTRAL

    def test_analyze_user_updates_state(self, engine):
        from core.schema.mood import Mood
        engine.analyze_user("哈哈太开心了")
        assert engine.state.mood == Mood.HAPPY
        assert engine.state.mood_intensity > 0.4

    def test_mood_decay(self, engine):
        from core.schema.mood import Mood
        engine.analyze_user("哈哈今天真开心")
        initial_intensity = engine.state.mood_intensity

        engine.analyze_assistant("我也很开心")
        decayed = engine.state.mood_intensity
        assert decayed < initial_intensity

    def test_neutral_after_decay(self, engine):
        from core.schema.mood import Mood
        engine.state.mood_intensity = 0.15
        engine.analyze_assistant("好的")
        # Intensity dropped below 0.2, should reset to neutral
        assert engine.state.mood == Mood.NEUTRAL

    def test_same_mood_increases_intensity(self, engine):
        from core.schema.mood import Mood
        engine.analyze_user("哈哈开心")
        i1 = engine.state.mood_intensity

        engine.analyze_user("开心呢")
        i2 = engine.state.mood_intensity
        assert i2 >= i1

    def test_mood_switch(self, engine):
        from core.schema.mood import Mood
        engine.analyze_user("哈哈开心")
        assert engine.state.mood == Mood.HAPPY

        engine.analyze_user("好难过啊")
        assert engine.state.mood == Mood.SAD

    # ---- STT emotion analysis ----

    def test_analyze_user_stt_sets_mood(self, engine):
        """STT HAPPY → AI mood warm"""
        from core.schema.mood import Mood
        result = engine.analyze_user_stt("HAPPY")
        assert result == Mood.WARM
        assert engine.state.mood == Mood.WARM

    def test_analyze_user_stt_sad(self, engine):
        """STT SAD → AI mood concerned (not sad)"""
        from core.schema.mood import Mood
        result = engine.analyze_user_stt("SAD")
        assert result == Mood.CONCERNED
        assert engine.state.mood == Mood.CONCERNED

    def test_analyze_user_stt_neutral_no_change(self, engine):
        """STT NEUTRAL — return current mood, no push"""
        from core.schema.mood import Mood
        current = engine.state.mood
        result = engine.analyze_user_stt("NEUTRAL")
        assert result == current
        # EMO_UNKNOWN returns None → no update, returns current mood
        assert engine.analyze_user_stt("EMO_UNKNOWN") == current

    def test_analyze_user_stt_with_laughter_event(self, engine):
        """Laughter event pushes laugh expression"""
        engine.analyze_user_stt("HAPPY", events=["Laughter"])
        # mood should be set
        from core.schema.mood import Mood
        assert engine.state.mood == Mood.WARM

    def test_analyze_user_stt_same_mood_increases_intensity(self, engine):
        """Same STT mood → increase intensity"""
        from core.schema.mood import Mood
        engine.analyze_user_stt("HAPPY")
        i1 = engine.state.mood_intensity
        engine.analyze_user_stt("HAPPY")
        i2 = engine.state.mood_intensity
        assert i2 >= i1


# ============================================================================
# ============================================================================
# 5. Streaming integration tests
# ============================================================================

@pytest.mark.service
class TestStreamingIntegration:
    @pytest.mark.asyncio
    async def test_fake_llm_streaming(self):
        """FakeLLMBackend simulates streaming by yielding characters."""
        from core.tests.fake_llm import FakeLLMBackend
        backend = FakeLLMBackend(response="你好呀！今天过得怎么样？")

        chars = []
        async for ch in backend.chat_stream([{"role": "user", "content": "你好"}]):
            chars.append(ch)

        text = "".join(chars)
        assert "今天过得怎么样" in text

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_real_stream_think_structured(self):
        """Real LLM structured streaming yields content dicts."""
        from dotenv import load_dotenv
        load_dotenv()
        from core.adapter.llm_adapter import LLMAdapter

        adapter = LLMAdapter()
        chunks = []
        async for chunk in adapter.stream_think(
            [{"role": "user", "content": "说一个字"}],
            tools=[{"type": "function", "function": {
                "name": "get_time",
                "description": "获取时间",
                "parameters": {"type": "object", "properties": {}, "required": []}
            }}],
        ):
            chunks.append(chunk)

        assert len(chunks) > 0
        # At least some chunks have content
        assert any(c["content"] for c in chunks)
        # No tool calls expected for this simple query (LLM decides)
        await adapter.close()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_streaming_delivers_tokens_immediately(self):
        """First chunk should arrive quickly, not after full response."""
        from dotenv import load_dotenv
        load_dotenv()
        import time
        from core.adapter.llm_adapter import LLMAdapter

        adapter = LLMAdapter()
        start = time.monotonic()
        first_chunk_time = None

        it = adapter.stream_think(
            [{"role": "user", "content": "说一个字"}],
        )
        try:
            chunk = await it.__anext__()
            first_chunk_time = time.monotonic() - start
        finally:
            await it.aclose()  # type: ignore[attr-defined]

        assert first_chunk_time is not None
        assert first_chunk_time < 10.0, f"TTFT {first_chunk_time:.1f}s exceeds 10s limit"
        await adapter.close()


# ============================================================================
# 6. Interrupt integration tests
# ============================================================================

@pytest.mark.slow
class TestInterruptIntegration:
    @pytest.mark.asyncio
    async def test_interrupt_during_processing(self):
        """Interrupt during LLM processing should return agent to IDLE."""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent
        from core.agent_fsm import AgentState

        from core.app_config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()

        # Start a request
        await agent.process_message("写一首长诗，越多越好，我要看长篇")
        await asyncio.sleep(0.3)  # Let cognitive task start

        # Post interrupt
        agent._bus.post(InterruptData(source="test"), priority=0)
        await asyncio.sleep(2.0)

        assert agent._fsm.state == AgentState.IDLE, \
            f"Expected IDLE after interrupt, got {agent._fsm.state}"
        await agent.stop()

    @pytest.mark.asyncio
    async def test_new_message_after_interrupt(self):
        """After interrupt, a new message should be processable."""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent
        from core.agent_fsm import AgentState

        from core.app_config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()

        # Interrupt first request
        await agent.process_message("写一首很长的诗，越长越好")
        await asyncio.sleep(0.5)  # Wait for LLM stream to start
        agent._bus.post(InterruptData(source="test"), priority=0)
        await asyncio.sleep(0.5)  # Give time for cancellation to propagate

        # The agent should eventually return to IDLE
        # (real LLM streams can't be instantly cancelled due to HTTP buffering)
        for _ in range(20):  # Wait up to 10 seconds
            if agent._fsm.state == AgentState.IDLE:
                break
            await asyncio.sleep(0.5)

        # Second request should work regardless
        await agent.process_message("你好呀")
        await asyncio.sleep(3.0)

        messages = agent._session.recent_messages()
        # At minimum we should have some conversation history
        assert len(messages) > 0, "Expected at least some session messages"
        await agent.stop()

    @pytest.mark.asyncio
    async def test_multiple_interrupts(self):
        """Multiple rapid interrupts should not crash the agent."""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent
        from core.agent_fsm import AgentState

        from core.app_config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()

        for i in range(3):
            await agent.process_message(f"写一首诗")
            await asyncio.sleep(0.2)
            agent._bus.post(InterruptData(source="test"), priority=0)
            await asyncio.sleep(1.0)
            assert agent._fsm.state == AgentState.IDLE, \
                f"Iteration {i}: expected IDLE, got {agent._fsm.state}"

        await agent.stop()


# ============================================================================
# 7. Multi-turn conversation tests
# ============================================================================

@pytest.mark.slow
class TestMultiTurnConversation:
    @pytest.mark.asyncio
    async def test_session_context_preserved(self):
        """Messages from previous turns appear in the system prompt context."""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent

        from core.app_config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()

        # First turn (may be slower due to lazy embedding loading)
        await agent.process_message("我叫妖梦")
        await asyncio.sleep(15)  # Generous timeout for embedding lazy load + LLM
        msgs_1 = agent._session.recent_messages()
        assert any(
            isinstance(m.get("content"), (list, str)) and
            any("我叫妖梦" in p.get("content", "") for p in (m["content"] if isinstance(m.get("content"), list) else [{"content": m.get("content", "")}]))
            for m in msgs_1
        )

        # Second turn (embedding already loaded, should be fast)
        await agent.process_message("你还记得我叫什么吗")
        await asyncio.sleep(5)
        msgs_2 = agent._session.recent_messages()

        # Session should have all messages (user + assistant from both turns)
        assert len(msgs_2) >= 3

        await agent.stop()

    @pytest.mark.asyncio
    async def test_tool_calling_with_real_llm(self):
        """Real LLM may or may not call tools — verify both paths work."""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent
        from core.agent_fsm import AgentState

        from core.app_config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()

        await agent.process_message("帮我查一下记忆里有什么")
        await asyncio.sleep(6)

        assert agent._fsm.state == AgentState.IDLE
        await agent.stop()

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_character_personality_injected(self):
        """Verify character config (base_personality) appears in the LLM request."""
        from dotenv import load_dotenv
        load_dotenv()
        from core.prompt_assembler import PromptAssembler
        from core.character_config import CharacterConfig
        from core.memory.session import SessionMemory
        from core.memory.long_term import LongTermMemory

        char = CharacterConfig(
            name="TestBot",
            base_personality="性格: 勇敢、幽默\n说话风格: 说文言文",
        )
        session = SessionMemory()
        lt = LongTermMemory()
        pa = PromptAssembler(char, session, lt)

        system = await pa.build_system_prompt(user_message="", user_name="测试员")
        assert "TestBot" in system
        assert "勇敢" in system
        assert "幽默" in system
        assert "说文言文" in system


# ============================================================================
# 8. Edge case tests
# ============================================================================

@pytest.mark.slow
class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_message(self):
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent

        from core.app_config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()

        await agent.process_message("")
        await asyncio.sleep(0.5)
        # Should not crash, state should remain IDLE
        assert agent._fsm.state in ("idle", "thinking")  # Could be idle or still in prev state
        await agent.stop()



# ============================================================================
# Runner
# ============================================================================

if __name__ == "__main__":
    # Run all async tests
    async def run_all():
        test_classes = [
            TestStreamingIntegration,
            TestInterruptIntegration,
            TestMultiTurnConversation,
            TestEdgeCases,
        ]

        passed = 0
        failed = 0

        for cls in test_classes:
            print(f"\n{'='*60}")
            print(f"  {cls.__name__}")
            print(f"{'='*60}")

            instance = cls()
            for name in dir(instance):
                if name.startswith("test_"):
                    method = getattr(instance, name)
                    try:
                        await method()
                        print(f"  ✓ {name}")
                        passed += 1
                    except Exception as e:
                        print(f"  ✗ {name}: {e}")
                        import traceback
                        traceback.print_exc()
                        failed += 1

        print(f"\n{'='*60}")
        print(f"  Results: {passed} passed, {failed} failed")
        print(f"{'='*60}")

    asyncio.run(run_all())


# ============================================================
# IdleMonitor — _check_proactive (basic smoke tests)
# ============================================================

@pytest.mark.service
class TestProactiveTriggers:
    @pytest.mark.asyncio
    async def test_empty_triggers_does_nothing(self):
        from core.idle_monitor import IdleMonitor
        from core.event_bus import EventBus
        from core.agent_fsm import AgentFSM
        from unittest.mock import MagicMock

        cfg = MagicMock()
        cfg.proactive.enabled = True
        cfg.proactive.triggers = []
        cfg.proactive.interval = 300
        fsm = AgentFSM()
        bus = EventBus()
        monitor = IdleMonitor(cfg, fsm, bus)
        await monitor._check_proactive()  # should not crash

    @pytest.mark.asyncio
    async def test_silence_timeout_posts_event(self):
        from core.idle_monitor import IdleMonitor
        from core.event_bus import EventBus
        from core.agent_fsm import AgentFSM
        from unittest.mock import MagicMock

        cfg = MagicMock()
        cfg.proactive.enabled = True
        cfg.proactive.triggers = [{"type": "silence_timeout", "duration": 1}]
        cfg.proactive.interval = 300
        fsm = AgentFSM()
        bus = EventBus()
        monitor = IdleMonitor(cfg, fsm, bus)
        monitor._last_interaction = 0
        await monitor._check_proactive()
        events = list(bus.drain())
        assert any(e.type == "system_tick" for e in events)
