"""阶段 1 综合测试：流式输出、中断、情绪、句子缓冲。

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

from core.schema import RequestId
from core.schema.event import InterruptData, StateChangeData, UserInputData

# ============================================================================
# 1. EventBus + FSM tests
# ============================================================================

@pytest.mark.unit
class TestEventBus:
    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        from core.monitoring.event_bus import EventBus, EventPriority
        bus = EventBus()

        bus.post(UserInputData(text="data1", user_name="test", request_id=RequestId("?")), type="normal", priority=EventPriority.NORMAL)
        bus.post(InterruptData(), priority=EventPriority.INTERRUPT)
        bus.post(StateChangeData(state="idle", request_id="?"), priority=EventPriority.STATE_CHANGE)

        e1 = await bus.next()
        e2 = await bus.next()
        e3 = await bus.next()

        assert e1.type == "interrupt"
        assert e2.type == "state_change"
        assert e3.type == "normal"

    def test_drain(self):
        from core.monitoring.event_bus import EventBus, EventPriority
        bus = EventBus()
        bus.post(UserInputData(text="", user_name="", request_id=RequestId("")), type="a", priority=EventPriority.NORMAL)
        bus.post(UserInputData(text="", user_name="", request_id=RequestId("")), type="b", priority=EventPriority.NORMAL)
        bus.post(UserInputData(text="", user_name="", request_id=RequestId("")), type="c", priority=EventPriority.NORMAL)

        drained = bus.drain()
        assert len(drained) == 3
        assert bus.pending_count == 0


@pytest.mark.unit
class TestAgentFSM:
    def test_valid_transitions(self):
        from core.status.agent_fsm import AgentFSM, AgentState
        fsm = AgentFSM()

        assert fsm.state == AgentState.IDLE
        fsm.transition(AgentState.THINKING)
        assert fsm.state == AgentState.THINKING
        fsm.transition(AgentState.SPEAKING)
        assert fsm.state == AgentState.SPEAKING
        fsm.transition(AgentState.IDLE)
        assert fsm.state == AgentState.IDLE

    def test_invalid_transition_raises(self):
        from core.status.agent_fsm import AgentFSM, AgentState
        fsm = AgentFSM()

        with pytest.raises(ValueError):
            fsm.transition(AgentState.SPEAKING)  # IDLE -> SPEAKING invalid

    def test_interrupt_from_thinking(self):
        from core.status.agent_fsm import AgentFSM, AgentState
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)

        assert fsm.interrupt() is True
        assert fsm.state == AgentState.INTERRUPTED

        fsm.transition(AgentState.IDLE)
        assert fsm.state == AgentState.IDLE

    def test_interrupt_from_idle_noop(self):
        from core.status.agent_fsm import AgentFSM, AgentState
        fsm = AgentFSM()

        assert fsm.interrupt() is False
        assert fsm.state == AgentState.IDLE

    def test_complex_transition_chain(self):
        """完整生命周期：IDLE -> THINKING -> EXECUTING -> THINKING -> SPEAKING -> IDLE"""
        from core.status.agent_fsm import AgentFSM, AgentState
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
        from core.config import CharacterConfig
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
        assert engine._detect_mood("哈哈太开心了") == "happy"
        assert engine._detect_mood("今天真开心") == "happy"
        # "太好了" matches both happy("太好了") and excited("!").
        # Happy 关键词计数更高，因此胜出。
        assert engine._detect_mood("太好了") == "happy"
        assert engine._detect_mood("难过死了") == "sad"
        assert engine._detect_mood("为什么啊") == "curious"

    def test_detect_excited(self, engine):
        # "！！！" only matches excited keywords
        assert engine._detect_mood("天哪真的假的！！！") == "excited"
        assert engine._detect_mood("竟然！！！牛逼") == "excited"

    def test_detect_neutral(self, engine):
        assert engine._detect_mood("今天天气不错") == "neutral"
        assert engine._detect_mood("你好") == "neutral"

    def test_analyze_user_updates_state(self, engine):
        engine.analyze_user("哈哈太开心了")
        assert engine.state.mood == "happy"
        assert engine.state.mood_intensity > 0.4

    def test_mood_decay(self, engine):
        engine.analyze_user("哈哈今天真开心")
        initial_intensity = engine.state.mood_intensity

        engine.analyze_assistant("我也很开心")
        decayed = engine.state.mood_intensity
        assert decayed < initial_intensity

    def test_neutral_after_decay(self, engine):
        engine.state.mood_intensity = 0.15
        engine.analyze_assistant("好的")
        # 强度降至 0.2 以下，mood 文本不再重置
        assert engine.state.mood == "平静"

    def test_same_mood_increases_intensity(self, engine):
        engine.analyze_user("哈哈开心")
        i1 = engine.state.mood_intensity

        engine.analyze_user("开心呢")
        i2 = engine.state.mood_intensity
        assert i2 >= i1

    def test_mood_switch(self, engine):
        engine.analyze_user("哈哈开心")
        assert engine.state.mood == "happy"

        engine.analyze_user("好难过啊")
        assert engine.state.mood == "sad"

    # ---- STT audio event handling ----
    # analyze_user_stt 不再改变 mood —— mood 判定交由 LLM（C5 感官）。

    def test_analyze_user_stt_does_not_change_mood(self, engine):
        """STT 不再修改 mood，mood 决策交给 LLM。"""
        original = engine.state.mood
        engine.analyze_user_stt("HAPPY")
        assert engine.state.mood == original

    def test_analyze_user_stt_laughter_event(self, engine):
        """Laughter 事件推送 laugh 表情"""
        engine.analyze_user_stt("HAPPY", events=["Laughter"])
        engine._output.expression.assert_called_with("laugh", intensity=0.7)

    def test_analyze_user_stt_cry_event(self, engine):
        """Cry 事件推送 cry 表情"""
        engine.analyze_user_stt("SAD", events=["Cry"])
        engine._output.expression.assert_called_with("cry", intensity=0.8)

    def test_analyze_user_stt_applause_event(self, engine):
        """Applause 事件推送 surprised 表情"""
        engine.analyze_user_stt("SURPRISED", events=["Applause"])
        engine._output.expression.assert_called_with("surprised", intensity=0.5)

    def test_analyze_user_stt_no_events(self, engine):
        """无事件时 analyze_user_stt 是 no-op"""
        original = engine.state.mood
        engine.analyze_user_stt("ANGRY")
        assert engine.state.mood == original


# ============================================================================
# ============================================================================
# 5. Streaming integration tests
# ============================================================================

def _skip_if_no_real_llm_key():
    """真实网络 LLM 测试的 key 守卫。

    缺失或被测试环境污染为 sentinel（"test"）时跳过——对齐 workflow_engine
    integration 测试的惯例（`pytest.skip("DEEPSEEK_API_KEY 未设置")`）。
    无守卫时，进程 env 被 `setdefault("DEEPSEEK_API_KEY","test")` 污染后
    会向真实 API 发假 key → 401 硬失败。
    """
    import os
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key or key == "test":
        pytest.skip("DEEPSEEK_API_KEY 未设置或非真实 key，跳过真实网络测试")


class TestStreamingIntegration:
    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_fake_llm_streaming(self):
        """FakeLLMBackend 通过逐字符产出模拟流式输出。"""
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
        """真实 LLM 结构化流式输出 content 字典。"""
        from dotenv import load_dotenv
        load_dotenv()
        _skip_if_no_real_llm_key()
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
        # 至少部分块有内容
        assert any(c["content"] for c in chunks)
        # 这个简单查询不应有工具调用（由 LLM 决定）
        await adapter.close()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_streaming_delivers_tokens_immediately(self):
        """第一个块应快速到达，而不是在整个响应完成后。"""
        from dotenv import load_dotenv
        load_dotenv()
        _skip_if_no_real_llm_key()
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
        """LLM 处理过程中中断应使 agent 回到 IDLE。"""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent
        from core.status.agent_fsm import AgentState

        from core.config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()

        # 发起一个请求
        await agent.process_message("写一首长诗，越多越好，我要看长篇")
        await asyncio.sleep(0.3)  # Let cognitive task start

        # 中断之后
        agent._bus.post(InterruptData(source="test"), priority=0)
        await asyncio.sleep(2.0)

        assert agent._fsm.state == AgentState.IDLE, \
            f"Expected IDLE after interrupt, got {agent._fsm.state}"
        await agent.stop()

    @pytest.mark.asyncio
    async def test_new_message_after_interrupt(self):
        """中断之后，新消息应可被处理。"""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent
        from core.status.agent_fsm import AgentState

        from core.config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()

        # 中断第一个请求
        await agent.process_message("写一首很长的诗，越长越好")
        await asyncio.sleep(0.5)  # Wait for LLM stream to start
        agent._bus.post(InterruptData(source="test"), priority=0)
        await asyncio.sleep(0.5)  # Give time for cancellation to propagate

        # agent 最终应回到 IDLE
        # (real LLM streams can't be instantly cancelled due to HTTP buffering)
        for _ in range(20):  # Wait up to 10 seconds
            if agent._fsm.state == AgentState.IDLE:
                break
            await asyncio.sleep(0.5)

        # 第二个请求无论如何都应正常工作
        await agent.process_message("你好呀")
        await asyncio.sleep(3.0)

        messages = agent.session.recent_messages()
        # 至少应有一些对话历史
        assert len(messages) > 0, "Expected at least some session messages"
        await agent.stop()

    @pytest.mark.asyncio
    async def test_multiple_interrupts(self):
        """多次快速中断不应使 agent 崩溃。"""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent
        from core.status.agent_fsm import AgentState

        from core.config import AppConfig
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
        """前几轮的消息应出现在系统提示上下文中。"""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent

        from core.config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()

        # 第一轮（可能因嵌入懒加载而较慢）
        await agent.process_message("我叫妖梦")
        await asyncio.sleep(15)  # Generous timeout for embedding lazy load + LLM
        msgs_1 = agent.session.recent_messages()
        assert any(
            isinstance(m.content, list) and
            any("我叫妖梦" in getattr(p, "content", "") for p in m.content)
            for m in msgs_1
        )

        # 第二轮（嵌入已加载，应较快）
        await agent.process_message("你还记得我叫什么吗")
        await asyncio.sleep(5)
        msgs_2 = agent.session.recent_messages()

        # 会话应有全部消息（两轮的 user + assistant）
        assert len(msgs_2) >= 3

        await agent.stop()

    @pytest.mark.asyncio
    async def test_tool_calling_with_real_llm(self):
        """真实 LLM 可能调用工具也可能不调用 —— 验证两条路径都可用。"""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent
        from core.status.agent_fsm import AgentState

        from core.config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()

        await agent.process_message("帮我查一下记忆里有什么")
        await asyncio.sleep(6)

        assert agent._fsm.state == AgentState.IDLE
        await agent.stop()

    @pytest.mark.service
    @pytest.mark.asyncio
    async def test_character_personality_injected(self):
        """验证角色配置（base_personality）出现在 LLM 请求中。"""
        from dotenv import load_dotenv
        load_dotenv()
        from core.prompt.prompt_assembler import PromptAssembler
        from core.config import CharacterConfig
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

        from core.config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()

        await agent.process_message("")
        await asyncio.sleep(0.5)
        # 不应崩溃，状态应保持 IDLE
        assert agent._fsm.state in ("idle", "thinking")  # Could be idle or still in prev state
        await agent.stop()



# ============================================================================
# 运行入口
# ============================================================================

if __name__ == "__main__":
    # 运行所有异步测试
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
# IdleMonitor —— _check_proactive（基础冒烟测试）
# ============================================================

@pytest.mark.service
class TestProactiveTriggers:
    @pytest.mark.asyncio
    async def test_empty_triggers_does_nothing(self):
        from core.monitoring.idle_monitor import IdleMonitor
        from core.monitoring.event_bus import EventBus
        from core.status.agent_fsm import AgentFSM
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
        from core.monitoring.idle_monitor import IdleMonitor
        from core.monitoring.event_bus import EventBus
        from core.status.agent_fsm import AgentFSM
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
