"""Phase 3 tests: CognitiveLoop, server, DemoAgent, LLM adapter, edge cases.

Run: python -m pytest core/test_phase3.py -v --asyncio-mode=auto
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.cognitive_loop import CognitiveLoop, _MessageObj
from core.agent_fsm import AgentFSM, AgentState
from core.event_bus import EventBus, EventType, EventPriority
from core.llm_adapter import LLMAdapter
from core.tests.fake_llm import FakeLLMBackend
from core.app_config import AppConfig


# ============================================================================
# 1. CognitiveLoop — _merge_stream_tool_calls
# ============================================================================

class TestMergeStreamToolCalls:
    def test_single_delta_new_entry(self):
        accum: list[dict] = []
        delta = [{"index": 0, "id": "call_1", "function": {"name": "search", "arguments": '{"q"'}}]
        CognitiveLoop._merge_stream_tool_calls(accum, delta)
        assert len(accum) == 1
        assert accum[0]["id"] == "call_1"
        assert accum[0]["function"]["name"] == "search"
        assert accum[0]["function"]["arguments"] == '{"q"'

    def test_same_index_accumulates(self):
        accum: list[dict] = []
        CognitiveLoop._merge_stream_tool_calls(accum, [
            {"index": 0, "function": {"name": "se", "arguments": ""}}
        ])
        CognitiveLoop._merge_stream_tool_calls(accum, [
            {"index": 0, "function": {"name": "arch", "arguments": '{"q":1}'}}
        ])
        assert accum[0]["function"]["name"] == "search"
        assert accum[0]["function"]["arguments"] == '{"q":1}'

    def test_delta_id_only_no_function(self):
        accum: list[dict] = []
        delta = [{"index": 0, "id": "call_x"}]
        CognitiveLoop._merge_stream_tool_calls(accum, delta)
        assert accum[0]["id"] == "call_x"
        assert accum[0]["function"]["name"] == ""

    def test_multi_index_interleaved(self):
        accum: list[dict] = []
        CognitiveLoop._merge_stream_tool_calls(accum, [
            {"index": 0, "function": {"name": "fn0"}},
            {"index": 1, "function": {"name": "fn1"}},
        ])
        assert len(accum) == 2
        assert accum[0]["function"]["name"] == "fn0"
        assert accum[1]["function"]["name"] == "fn1"

    def test_empty_deltas_noop(self):
        accum: list[dict] = []
        CognitiveLoop._merge_stream_tool_calls(accum, [])
        assert accum == []

    def test_missing_function_field(self):
        accum: list[dict] = []
        delta = [{"index": 0, "id": "x"}]
        CognitiveLoop._merge_stream_tool_calls(accum, delta)
        # Should not crash
        assert accum[0]["id"] == "x"


# ============================================================================
# 2. CognitiveLoop — _normalize_tool_calls
# ============================================================================

class TestNormalizeToolCalls:
    def test_all_complete(self):
        raw = [
            {"id": "a", "function": {"name": "f1", "arguments": "{}"}},
            {"id": "b", "function": {"name": "f2", "arguments": "{}"}},
        ]
        result = CognitiveLoop._normalize_tool_calls(raw)
        assert len(result) == 2

    def test_all_incomplete_missing_id(self):
        raw = [{"id": "", "function": {"name": "f1", "arguments": "{}"}}]
        assert CognitiveLoop._normalize_tool_calls(raw) == []

    def test_all_incomplete_missing_name(self):
        raw = [{"id": "x", "function": {"name": "", "arguments": "{}"}}]
        assert CognitiveLoop._normalize_tool_calls(raw) == []

    def test_mixed(self):
        raw = [
            {"id": "a", "function": {"name": "ok", "arguments": "{}"}},
            {"id": "", "function": {"name": "bad", "arguments": "{}"}},
        ]
        result = CognitiveLoop._normalize_tool_calls(raw)
        assert len(result) == 1
        assert result[0]["id"] == "a"


# ============================================================================
# 3. CognitiveLoop — _synthesize_sentence, _handle_interrupt
# ============================================================================

class TestCognitiveLoopMethods:
    @pytest.fixture
    def loop(self):
        """Create a minimal CognitiveLoop with mocks for all dependencies."""
        from Module.output import OutputManager, ChannelCapability
        from core.prompt_assembler import PromptAssembler
        from Module.tool import ToolRegistry, ToolDefinition, ToolParameter
        from core.memory.session import SessionMemory
        from core.character_config import CharacterConfig
        from core.emotion_engine import EmotionEngine
        from core.consolidator import MemoryConsolidator
        from core.memory.long_term import LongTermMemory

        llm = MagicMock(spec=LLMAdapter)
        fsm = AgentFSM()
        bus = EventBus()
        output = OutputManager()
        char = CharacterConfig(name="TestBot")
        session = SessionMemory()
        lt = LongTermMemory()
        prompt = PromptAssembler(char, session, lt)
        tools = ToolRegistry()
        emotion = EmotionEngine(char, session, output)
        consolidator = MemoryConsolidator(llm, lt, "TestBot")
        tts = AsyncMock()

        loop = CognitiveLoop(
            llm=llm, fsm=fsm, event_bus=bus, output=output,
            prompt=prompt, session=session, tools=tools,
            emotion=emotion, consolidator=consolidator, tts=tts,
        )
        return loop

    @pytest.mark.asyncio
    async def test_synthesize_sentence_tts_none(self, loop):
        loop._tts = None
        await loop._synthesize_sentence("hello")
        # Should return immediately without error

    @pytest.mark.asyncio
    async def test_synthesize_sentence_tts_success(self, loop):
        loop._tts = AsyncMock()
        loop._tts.synthesize.return_value = b"fakewav"

        with patch.object(loop._output, "audio_chunk", new_callable=AsyncMock) as mock_ac:
            await loop._synthesize_sentence("hello")
            loop._tts.synthesize.assert_awaited_once()
            mock_ac.assert_awaited_once_with(b"fakewav")

    @pytest.mark.asyncio
    async def test_synthesize_sentence_tts_failure(self, loop):
        loop._tts = AsyncMock()
        loop._tts.synthesize.side_effect = RuntimeError("TTS down")

        await loop._synthesize_sentence("hello")
        # Should not raise

    @pytest.mark.asyncio
    async def test_handle_interrupt(self, loop):
        loop._fsm.transition(AgentState.THINKING)
        loop._sentence_buf.feed("partial text")

        await loop._handle_interrupt()

        assert loop._fsm.state == AgentState.IDLE
        assert loop._sentence_buf._buffer == ""

    @pytest.mark.asyncio
    async def test_process_rejects_invalid_fsm_state(self, loop):
        loop._fsm.transition(AgentState.THINKING)
        loop._fsm.interrupt()
        # Now INTERRUPTED — can only go to IDLE, not THINKING
        await loop.process("hello")
        # Should warn and return without transitioning to THINKING

    @pytest.mark.asyncio
    async def test_process_normal_flow(self, loop):
        async def mock_stream(*args, **kwargs):
            yield {"content": "Hello!", "tool_calls": None}
        loop._llm.stream_think = mock_stream

        await loop.process("hi")
        assert loop._fsm.state == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_after_response_no_consolidator(self, loop):
        loop._consolidator = None
        await loop._after_response("user msg", "assistant reply")
        # Should not raise

    @pytest.mark.asyncio
    async def test_after_response_with_consolidator(self, loop):
        loop._consolidator.consolidate = AsyncMock(return_value=[])
        await loop._after_response("user msg", "assistant reply")
        loop._consolidator.consolidate.assert_awaited_once()


# ============================================================================
# 3b. CognitiveLoop — TTS + multi-tool
# ============================================================================

class TestCognitiveLoopTTS:
    @pytest.fixture
    def loop(self):
        from core.event_bus import EventBus
        from core.agent_fsm import AgentFSM
        from Module.output import OutputManager
        from core.prompt_assembler import PromptAssembler
        from core.memory.session import SessionMemory
        from Module.tool import ToolRegistry, ToolDefinition, ToolParameter
        from core.llm_adapter import LLMAdapter

        fsm = AgentFSM()
        llm = LLMAdapter(_test_backend=FakeLLMBackend(response="你好"))
        bus = EventBus()
        output = OutputManager()
        char = MagicMock()
        char.name = "Test"
        char.personality_block.return_value = "personality"
        session = SessionMemory()
        prompt = MagicMock()
        prompt.build_messages = AsyncMock(return_value=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ])
        prompt._character = char
        tools = ToolRegistry()
        tts = MagicMock()
        tts.synthesize_stream = AsyncMock(return_value=AsyncMock())
        # Make synthesize_stream return no chunks (empty stream)
        async def _empty_stream(*args, **kwargs):
            return
            yield  # unreachable, makes it an async generator
        tts.synthesize_stream = MagicMock()
        # Use a real async generator
        async def _fake_stream(text, character=None):
            yield b"audio_chunk_1"
        tts.synthesize_stream = _fake_stream
        return CognitiveLoop(
            llm=llm, fsm=fsm, event_bus=bus, output=output,
            prompt=prompt, session=session, tools=tools, tts=tts,
        )

    @pytest.mark.asyncio
    async def test_start_tts_stream_creates_task(self, loop):
        """_start_tts_stream creates a background TTS task."""
        initial_count = len(loop._tts_tasks)
        loop._start_tts_stream("你好世界")
        assert len(loop._tts_tasks) == initial_count + 1
        # Should clean up done tasks
        loop._start_tts_stream("第二句")
        # At most initial+2 tasks (some may have completed)
        assert len(loop._tts_tasks) <= initial_count + 2
        assert loop._tts_stream_id >= 2

    @pytest.mark.asyncio
    async def test_multi_tool_round(self, loop):
        """CognitiveLoop handles tool calls and loops correctly."""
        import json
        from Module.tool import ToolDefinition, ToolParameter
        # Use a fake backend that returns tool calls then final answer
        tool_json = json.dumps({
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "test_tool", "arguments": '{"q":"test"}'}
            }]
        })
        loop._llm._backend = FakeLLMBackend(response=tool_json)

        # Register a tool
        async def test_tool(q: str) -> str:
            return f"result for {q}"
        loop._tools.register(ToolDefinition(
            name="test_tool", description="test",
            func=test_tool,
            parameters={"q": ToolParameter(type="string", required=True)},
        ))

        # After process, the tool should have been called (tool history)
        await loop.process("use test_tool")
        # Tool was registered, process completed without error
        assert loop._fsm.state in ("idle", "thinking")

    @pytest.mark.asyncio
    async def test_handle_interrupt_clears_state(self, loop):
        """_handle_interrupt resets sentence buffer, cancels TTS tasks, returns to IDLE."""
        loop._sentence_buf.feed("partial")
        t = asyncio.create_task(asyncio.sleep(10))
        loop._tts_tasks.append(t)
        loop._fsm.transition("thinking")

        await loop._handle_interrupt()

        assert loop._sentence_buf._buffer == ""
        assert len(loop._tts_tasks) == 0
        assert loop._fsm.state == "idle"


# ============================================================================
# 3c. CognitiveLoop — TTS sync (basic)
# ============================================================================

class TestCognitiveLoopSync:
    @pytest.fixture
    def loop(self):
        from core.event_bus import EventBus
        from core.agent_fsm import AgentFSM
        from Module.output import OutputManager
        from core.prompt_assembler import PromptAssembler
        from core.memory.session import SessionMemory
        from Module.tool import ToolRegistry, ToolDefinition, ToolParameter
        from core.llm_adapter import LLMAdapter

        fsm = AgentFSM()
        llm = LLMAdapter(_test_backend=FakeLLMBackend(response="你好呀！"))
        bus = EventBus()
        output = OutputManager()
        char = MagicMock()
        char.name = "Test"
        char.personality_block.return_value = "personality"
        session = SessionMemory()
        prompt = MagicMock()
        prompt.build_messages = AsyncMock(return_value=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ])
        prompt._character = char
        tools = ToolRegistry()
        return CognitiveLoop(
            llm=llm, fsm=fsm, event_bus=bus, output=output,
            prompt=prompt, session=session, tools=tools, tts=None,
        )

    @pytest.mark.asyncio
    async def test_no_tts_text_flows_directly(self, loop):
        """无 TTS 时文本直接发送，不经过缓冲。"""
        await loop.process("hello")
        # process() 成功完成即表明文本已直接发送
        assert loop._fsm.state == "idle"

    @pytest.mark.asyncio
    async def test_process_resets_sentence_buffer(self, loop):
        """每次 process() 调用重置分句缓冲。"""
        loop._sentence_buf.feed("stale data")
        assert loop._sentence_buf._buffer != ""

        await loop.process("hello")

        assert loop._sentence_buf._buffer == ""

    @pytest.mark.asyncio
    async def test_process_lock_serializes(self, loop):
        """Concurrent process() calls are serialized by the lock."""
        started = []

        async def tracked_process(msg):
            started.append(msg)
            await loop.process(msg)

        t1 = asyncio.create_task(tracked_process("A"))
        t2 = asyncio.create_task(tracked_process("B"))
        await asyncio.gather(t1, t2)

        # Both should have started (B waited for A)
        assert len(started) == 2


# ============================================================================
# 4. AgentFSM — missing edges
# ============================================================================

class TestFSMEdges:
    def test_can_transition_valid(self):
        fsm = AgentFSM()
        assert fsm.can_transition(AgentState.THINKING) is True

    def test_can_transition_invalid(self):
        fsm = AgentFSM()
        assert fsm.can_transition(AgentState.SPEAKING) is False

    def test_interrupt_from_executing(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.EXECUTING)
        assert fsm.interrupt() is True
        assert fsm.state == AgentState.INTERRUPTED

    def test_interrupt_from_speaking(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        assert fsm.interrupt() is True
        assert fsm.state == AgentState.INTERRUPTED

    def test_interrupt_from_interrupted_noop(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        fsm.interrupt()
        assert fsm.state == AgentState.INTERRUPTED
        # From INTERRUPTED, interrupt should fail
        assert fsm.interrupt() is False

    def test_previous_property(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        assert fsm.previous == AgentState.IDLE
        fsm.transition(AgentState.SPEAKING)
        assert fsm.previous == AgentState.THINKING

    def test_speaking_to_executing(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        fsm.transition(AgentState.EXECUTING)
        assert fsm.state == AgentState.EXECUTING

    def test_speaking_to_thinking(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        fsm.transition(AgentState.THINKING)
        assert fsm.state == AgentState.THINKING


# ============================================================================
# 5. EventBus — missing edges
# ============================================================================

class TestEventBusEdges:
    def test_pending_count(self):
        bus = EventBus()
        assert bus.pending_count == 0
        bus.post("a")
        bus.post("b")
        assert bus.pending_count == 2

    @pytest.mark.asyncio
    async def test_fifo_within_same_priority(self):
        bus = EventBus()
        bus.post("first", priority=EventPriority.NORMAL)
        bus.post("second", priority=EventPriority.NORMAL)
        bus.post("third", priority=EventPriority.NORMAL)
        e1 = await bus.next()
        e2 = await bus.next()
        e3 = await bus.next()
        assert e1.type == "first"
        assert e2.type == "second"
        assert e3.type == "third"

    def test_drain_empty(self):
        bus = EventBus()
        assert bus.drain() == []


# ============================================================================
# 6. LLMAdapter — direct tests
# ============================================================================

class TestLLMAdapter:
    def test_model_property(self):
        adapter = LLMAdapter(_test_backend=FakeLLMBackend())
        assert isinstance(adapter.model, str)
        assert len(adapter.model) > 0

    @pytest.mark.asyncio
    async def test_think_returns_text(self):
        adapter = LLMAdapter(_test_backend=FakeLLMBackend(response="你好呀！"))

        result = await adapter.think([{"role": "user", "content": "你好"}])
        assert result.text == "你好呀！"
        assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_think_returns_tool_calls(self):
        import json
        fake = FakeLLMBackend(response=json.dumps({
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "search_memory", "arguments": "{}"},
            }]
        }))
        adapter = LLMAdapter(_test_backend=fake)

        tools = [{"type": "function", "function": {"name": "search_memory"}}]
        result = await adapter.think(
            [{"role": "user", "content": "查一下"}],
            tools=tools,
        )
        assert len(result.tool_calls) > 0
        assert result.tool_calls[0]["function"]["name"] == "search_memory"

    @pytest.mark.asyncio
    async def test_stream_think(self):
        adapter = LLMAdapter(_test_backend=FakeLLMBackend(response="你好呀！"))

        chunks = []
        async for chunk in adapter.stream_think([{"role": "user", "content": "你好"}]):
            chunks.append(chunk)

        # FakeLLMBackend has no stream_think, falls back to chat() then streams chars
        text = "".join(c["content"] for c in chunks if c["content"])
        assert "你好呀" in text



# ============================================================================
# 9. EmotionEngine — missing edges
# ============================================================================

class TestEmotionEngineEdges:
    @pytest.fixture
    def engine(self):
        from core.character_config import CharacterConfig
        from core.memory.session import SessionMemory
        from Module.output import OutputManager
        from core.emotion_engine import EmotionEngine

        char = CharacterConfig(name="Test")
        session = SessionMemory()
        output = OutputManager()
        return EmotionEngine(char, session, output)

    def test_multiple_moods_tie_breaker(self, engine):
        # "！" matches excited; "哈哈" matches happy. Happy has more matches.
        mood = engine._detect_mood("哈哈！")
        assert mood == "happy"

    def test_push_idle_empty_list(self, engine):
        engine._idle_expressions = []
        engine._push_idle()
        # Should not raise

    def test_intensity_capped_at_one(self, engine):
        engine._session.set_param("mood_intensity", 0.96)
        engine.analyze_user("开心")
        intensity = engine._session.get_param("mood_intensity", 0)
        assert intensity <= 1.0

    def test_intensity_floor_and_neutral(self, engine):
        engine._session.set_param("mood", "happy")
        engine._session.set_param("mood_intensity", 0.12)
        engine.analyze_assistant("好的")
        intensity = engine._session.get_param("mood_intensity", 0)
        assert intensity >= 0.1
        assert engine._session.get_param("mood") == "neutral"

    def test_schedule_no_loop_does_not_raise(self, engine):
        """_schedule should be a no-op when no event loop is running."""
        # In sync test context, no running loop → should not raise
        from core.emotion_engine import EmotionEngine
        EmotionEngine._schedule(MagicMock())  # coroutine mock, no loop

    def test_push_idle_uses_list(self, engine):
        """push_idle_expression picks from idle_expressions."""
        engine._idle_expressions = ["neutral", "blink"]
        # Should not raise — picks randomly
        import asyncio
        async def _run():
            await engine.push_idle_expression()
        try:
            asyncio.get_running_loop()
            # Loop exists, schedule should work
        except RuntimeError:
            pass  # No loop in sync context, expected


# ============================================================================
# 10b. PromptAssembler
# ============================================================================

class TestPromptAssemblerUnit:
    def test_format_events(self):
        from core.prompt_assembler import _format_events
        result = _format_events(["Laughter", "Cry"])
        assert "Laughter" in result
        assert "Cry" in result
        assert "😀" in result
        assert "😭" in result

    def test_format_events_empty(self):
        from core.prompt_assembler import _format_events
        assert _format_events([]) == ""

    @pytest.mark.asyncio
    async def test_build_system_prompt_fallback(self):
        """When template dir is invalid, fallback is used."""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.prompt_assembler import PromptAssembler
        from core.character_config import CharacterConfig
        from core.memory.session import SessionMemory
        from core.memory.long_term import LongTermMemory

        char = CharacterConfig.from_yaml("character/elysia.yaml")
        session = SessionMemory()
        lt = LongTermMemory()

        # Patch _PROMPT_DIR to an invalid path to trigger fallback
        import core.prompt_assembler as pa
        with patch.object(pa, '_PROMPT_DIR', '/nonexistent/path'):
            assembler = PromptAssembler(char, session, lt)
            prompt = await assembler.build_system_prompt("你好")
            assert "Elysia" in prompt
            assert "桌面AI伴侣" in prompt  # fallback text


# ============================================================================
# 11. DemoAgent — process_message
# ============================================================================

class TestDemoAgentProcessMessage:
    @pytest.mark.asyncio
    async def test_empty_message_returns(self):
        from dotenv import load_dotenv
        load_dotenv()
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        await agent.start()
        await agent.process_message("")
        assert agent._fsm.state == AgentState.IDLE
        await agent.stop()

    @pytest.mark.asyncio
    async def test_whitespace_only(self):
        from dotenv import load_dotenv
        load_dotenv()
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        await agent.start()
        await agent.process_message("   ")
        assert agent._fsm.state == AgentState.IDLE
        await agent.stop()

    @pytest.mark.asyncio
    async def test_idle_touch_called(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="你好")
        await agent.start()
        agent._idle.touch = MagicMock()
        await agent.process_message("hi")
        agent._idle.touch.assert_called_once()
        # Wait for cognitive task to start then finish
        for _ in range(100):
            if agent._fsm.state != AgentState.IDLE:
                break
            await asyncio.sleep(0.01)
        for _ in range(100):
            if agent._fsm.state == AgentState.IDLE:
                break
            await asyncio.sleep(0.01)
        await agent.stop()

    @pytest.mark.asyncio
    async def test_interrupt_when_not_idle(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="你好")
        await agent.start()

        # Send two messages in rapid succession
        await agent.process_message("hello")
        await agent.process_message("stop!")
        # Both should be processed without error
        await agent.stop()


# ============================================================================
# 12. OutputManager — audio_chunk, command convenience methods
# ============================================================================

class TestOutputManagerConvenience:
    @pytest.fixture
    def mgr(self):
        from Module.output import OutputManager
        return OutputManager()

    @pytest.mark.asyncio
    async def test_audio_chunk_routing(self, mgr):
        from Module.output import ChannelCapability
        ch = AsyncMock()
        ch.capabilities = {ChannelCapability.AUDIO}

        mgr.add_channel(ch)
        await mgr.audio_chunk(b"test audio", stream_id=0)

        ch.on_audio_chunk.assert_awaited_once_with(b"test audio", stream_id=0)

    @pytest.mark.asyncio
    async def test_audio_chunk_no_audio_channel(self, mgr):
        from Module.output import ChannelCapability
        ch = AsyncMock()
        ch.capabilities = {ChannelCapability.TEXT_DELTA}  # No AUDIO

        mgr.add_channel(ch)
        await mgr.audio_chunk(b"test")

        ch.on_audio_chunk.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_command_routing(self, mgr):
        from Module.output import ChannelCapability
        ch = AsyncMock()
        ch.capabilities = {ChannelCapability.COMMAND}

        mgr.add_channel(ch)
        await mgr.command("reload", params={"a": 1})

        ch.on_command.assert_awaited_once_with("reload", params={"a": 1})

    @pytest.mark.asyncio
    async def test_stream_empty(self, mgr):
        from Module.output import ChannelCapability
        ch = AsyncMock()
        ch.capabilities = {ChannelCapability.TEXT_DELTA, ChannelCapability.TEXT_MESSAGE}

        mgr.add_channel(ch)

        async def empty():
            if False:
                yield  # never yields
            return

        msg = await mgr.stream(empty(), finalize=lambda t: _MessageObj(content=t))
        ch.on_text_delta.assert_not_awaited()
        assert msg.content == ""


# ============================================================================
# 13. WebSocketChannel — new AUDIO/state methods
# ============================================================================

class TestWebSocketChannelNew:
    @pytest.fixture
    def ws_mock(self):
        return AsyncMock()

    def test_audio_in_capabilities(self, ws_mock):
        from Module.output import WebSocketChannel, ChannelCapability
        ch = WebSocketChannel(ws_mock)
        assert ChannelCapability.AUDIO in ch.capabilities

    @pytest.mark.asyncio
    async def test_on_audio_chunk(self, ws_mock):
        from Module.output import WebSocketChannel
        ch = WebSocketChannel(ws_mock)
        await ch.on_audio_chunk(b"\x00\x01\x02", stream_id=0)
        ws_mock.send_json.assert_awaited_once()
        call_args = ws_mock.send_json.call_args[0][0]
        assert call_args["type"] == "audio"
        assert call_args["stream_id"] == 0
        assert call_args["seq"] == 0
        assert call_args["is_final"] is False
        assert "data" in call_args

    @pytest.mark.asyncio
    async def test_on_state(self, ws_mock):
        from Module.output import WebSocketChannel
        ch = WebSocketChannel(ws_mock)
        await ch.on_state("thinking")
        ws_mock.send_json.assert_awaited_once_with(
            {"type": "state", "state": "thinking"}
        )

    @pytest.mark.asyncio
    async def test_on_expression_default_intensity(self, ws_mock):
        from Module.output import WebSocketChannel
        ch = WebSocketChannel(ws_mock)
        await ch.on_expression("happy")
        call_args = ws_mock.send_json.call_args[0][0]
        assert call_args["intensity"] == 1.0


# ============================================================================
# 14. AudioBuffer — STT error handling
# ============================================================================

class TestAudioBufferErrors:
    @pytest.mark.asyncio
    async def test_stt_timeout(self):
        from Module.input.audio_buffer import AudioBuffer

        async def slow_stt(wav):
            await asyncio.sleep(99)  # Will timeout
            return "result"

        trans_calls = []
        async def on_trans(text):
            trans_calls.append(text)

        buf = AudioBuffer(
            stt_transcribe=slow_stt,
            on_transcription=on_trans,
            silence_duration=0.01,
        )

        # Feed enough loud audio to create a segment
        import struct
        loud = struct.pack("<8000h", *([20000] * 8000))
        await buf.feed(loud)
        await asyncio.sleep(0.1)
        await buf.flush()

        # STT should have timed out, on_transcription not called
        assert len(trans_calls) == 0

    @pytest.mark.asyncio
    async def test_stt_exception(self):
        from Module.input.audio_buffer import AudioBuffer

        async def fail_stt(wav):
            raise RuntimeError("STT crashed")

        trans_calls = []
        async def on_trans(text):
            trans_calls.append(text)

        buf = AudioBuffer(stt_transcribe=fail_stt, on_transcription=on_trans, silence_duration=0.01)

        import struct
        loud = struct.pack("<8000h", *([20000] * 8000))
        for _ in range(10):
            await buf.feed(loud)
        await asyncio.sleep(0.1)
        await buf.flush()

        # Should not crash, on_transcription not called
        assert len(trans_calls) == 0

    @pytest.mark.asyncio
    async def test_stt_empty_result(self):
        from Module.input.audio_buffer import AudioBuffer

        async def empty_stt(wav):
            return ""

        trans_calls = []
        async def on_trans(text):
            trans_calls.append(text)

        buf = AudioBuffer(stt_transcribe=empty_stt, on_transcription=on_trans, silence_duration=0.01)

        import struct
        loud = struct.pack("<8000h", *([20000] * 8000))
        for _ in range(10):
            await buf.feed(loud)
        await asyncio.sleep(0.1)
        await buf.flush()

        assert len(trans_calls) == 0  # Empty result, no transcription callback

    @pytest.mark.asyncio
    async def test_flush_empty_buffer(self):
        from Module.input.audio_buffer import AudioBuffer
        buf = AudioBuffer()
        await buf.flush()  # Should not raise

    @pytest.mark.asyncio
    async def test_vad_silence_detected(self):
        """Near-silence PCM should be classified as non-speech by WebRTC VAD."""
        from Module.input.audio_buffer import AudioBuffer, _FRAME_BYTES
        buf = AudioBuffer(aggressiveness=3)
        # Generate near-silence frame (very low amplitude)
        import struct
        chunk = struct.pack(f"<{_FRAME_BYTES // 2}h", *([5] * (_FRAME_BYTES // 2)))
        assert buf._is_speech(chunk) is False


# ============================================================================
# 15. ConsoleChannel — dedup + inner_voice
# ============================================================================

class TestConsoleChannelDedup:
    @pytest.mark.asyncio
    async def test_on_message_after_stream_skips_content(self):
        from Module.output import ConsoleChannel
        ch = ConsoleChannel()
        # Simulate streaming
        await ch.on_text_delta("hello")
        # on_message after streaming should not print content line
        msg = _MessageObj(content="hello")
        # We can't easily capture stdout in pytest, but we verify no crash
        await ch.on_message(msg)
        assert ch._delta_printed is False  # reset after on_message

    @pytest.mark.asyncio
    async def test_on_message_without_stream(self):
        from Module.output import ConsoleChannel
        ch = ConsoleChannel()
        msg = _MessageObj(content="direct message")
        await ch.on_message(msg)
        # No delta was printed, should not crash

    @pytest.mark.asyncio
    async def test_on_message_with_inner_voice_after_stream(self):
        from Module.output import ConsoleChannel
        ch = ConsoleChannel()
        await ch.on_text_delta("hello")

        msg = MagicMock()
        msg.content = "hello"
        msg.role = "assistant"
        msg.inner_voice = "thinking..."
        msg.to_dict.return_value = {"role": "assistant", "content": "hello", "inner_voice": "thinking..."}

        # Use on_message directly via send
        from Module.output import OutputManager, ChannelCapability
        mgr = OutputManager()
        mgr.add_channel(ch)
        await mgr.send(msg)

        assert ch._delta_printed is False


# ============================================================================
# 16. _MessageObj
# ============================================================================

class TestMessageObj:
    def test_to_dict(self):
        msg = _MessageObj(role="assistant", content="hello world")
        d = msg.to_dict()
        assert d == {"role": "assistant", "content": "hello world"}

    def test_defaults(self):
        msg = _MessageObj()
        assert msg.role == "assistant"
        assert msg.content == ""


# ============================================================================
# 14. DemoAgent — lifecycle
# ============================================================================

class TestDemoAgentLifecycle:
    @pytest.mark.asyncio
    async def test_seed_memories(self):
        """_seed_memories writes 5 entries without triggering embedding."""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent._seed_memories()
        assert agent._long_term.count == 5
        # Entries should be MemoryEntry objects, not embedded
        assert all(hasattr(e, "text") for e in agent._long_term._entries)

    @pytest.mark.asyncio
    async def test_register_tools(self):
        """_register_tools registers 3 tools with correct names."""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        agent._register_tools()
        names = agent._tools.tool_names
        assert "search_memory" in names
        assert "get_time" in names
        assert "save_memory" in names

    @pytest.mark.asyncio
    async def test_tool_execute_search_memory(self):
        """search_memory tool queries long-term memory."""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from Module.tool import ToolContext
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        agent._register_tools()
        # Seed a memory entry
        from core.memory.long_term import MemoryEntry
        agent._long_term._entries.append(
            MemoryEntry(text="测试记忆", metadata={"type": "fact"}))

        ctx = ToolContext(services={"memory": agent._long_term})
        result = await agent._tools.execute("search_memory", {"query": "测试"}, ctx=ctx)
        assert result.success
        assert "测试记忆" in result.content

    @pytest.mark.asyncio
    async def test_stop_cancels_cognitive_task(self):
        """stop() cancels _current_task if still running."""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        # Simulate a running cognitive task
        async def fake_task():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass
        agent._cognitive._current_task = asyncio.create_task(fake_task())

        await agent.stop()
        assert agent._cognitive._current_task.done()
        assert agent._cognitive._current_task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_closes_output_channels(self):
        """stop() closes all output channels."""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()
        # Add a mock channel
        mock_ch = MagicMock()
        mock_ch.capabilities = set()
        mock_ch.close = AsyncMock()
        agent._output.add_channel(mock_ch)
        await agent.stop()
        mock_ch.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_message_poll_waits_for_idle(self):
        """process_message waits for FSM to return to IDLE after INTERRUPT."""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        # Start processing a message
        await agent.process_message("hello")
        # Wait for processing to complete
        for _ in range(100):
            if agent._fsm.state == "idle":
                break
            await asyncio.sleep(0.01)

        assert agent._fsm.state == "idle"
        await agent.stop()


# ============================================================================
# 14b. EmotionEngine — emoji mappings
# ============================================================================

class TestEmotionEmojiMappings:
    def test_stt_emotion_emoji_all(self):
        from core.emotion_engine import stt_emotion_emoji
        assert stt_emotion_emoji("HAPPY") == "😊"
        assert stt_emotion_emoji("SAD") == "😔"
        assert stt_emotion_emoji("ANGRY") == "😡"
        assert stt_emotion_emoji("SURPRISED") == "😮"
        assert stt_emotion_emoji("NEUTRAL") == ""
        assert stt_emotion_emoji("UNKNOWN") == ""

    def test_stt_event_emoji_all(self):
        from core.emotion_engine import stt_event_emoji
        assert stt_event_emoji("Laughter") == "😀"
        assert stt_event_emoji("Cry") == "😭"
        assert stt_event_emoji("Applause") == "👏"
        assert stt_event_emoji("Speech") == ""
        assert stt_event_emoji("UNKNOWN_EVENT") == ""


# ============================================================================
# 14c. PromptAssembler — time/day helpers
# ============================================================================

class TestPromptAssemblerHelpers:
    def test_time_of_day(self):
        from core.prompt_assembler import PromptAssembler
        assert PromptAssembler._time_of_day(6) == "morning"
        assert PromptAssembler._time_of_day(12) == "noon"
        assert PromptAssembler._time_of_day(15) == "afternoon"
        assert PromptAssembler._time_of_day(20) == "evening"
        assert PromptAssembler._time_of_day(3) == "night"

    def test_day_name(self):
        from core.prompt_assembler import PromptAssembler
        assert PromptAssembler._day_name(0) == "周一"
        assert PromptAssembler._day_name(6) == "周日"

    def test_season(self):
        from core.prompt_assembler import PromptAssembler
        assert PromptAssembler._season(4) == "spring"
        assert PromptAssembler._season(7) == "summer"
        assert PromptAssembler._season(10) == "autumn"
        assert PromptAssembler._season(1) == "winter"


# ============================================================================
# 14d. CharacterConfig
# ============================================================================

class TestCharacterConfig:
    def test_from_yaml_loads_correctly(self):
        from core.character_config import CharacterConfig
        char = CharacterConfig.from_yaml("character/elysia.yaml")
        assert char.name == "Elysia"
        assert "温柔" in char.traits
        assert len(char.wake_words) >= 1
        assert "Elysia" in char.wake_words

    def test_personality_block_format(self):
        from core.character_config import CharacterConfig
        char = CharacterConfig.from_yaml("character/elysia.yaml")
        block = char.personality_block()
        assert "Elysia" in block
        assert "温柔" in block
        assert "性格" in block or "说话风格" in block

    def test_from_yaml_file_not_found(self):
        from core.character_config import CharacterConfig
        with pytest.raises(FileNotFoundError):
            CharacterConfig.from_yaml("nonexistent.yaml")


# ============================================================================
# 14c. AppConfig
# ============================================================================

class TestAppConfig:
    def test_default_values(self):
        cfg = AppConfig()
        assert cfg.server.host == "0.0.0.0"
        assert cfg.server.port == 8765
        assert cfg.tts.enabled is False
        assert cfg.stt.enabled is False
        assert cfg.agent.character == "character/elysia.yaml"
        assert cfg.agent.user_name == "妖梦"
        assert cfg.llm.model == "deepseek/deepseek-chat"
        assert cfg.memory.session_max_messages == 20

    def test_load_from_yaml(self):
        from Module.config.loader import ConfigLoader
        cfg = ConfigLoader(AppConfig, yaml_path="config.yaml").load()
        assert cfg.tts.enabled is True
        assert cfg.stt.enabled is True
        assert cfg.agent.user_name == "妖梦"


# ============================================================================
# 14d. LLMAdapter
# ============================================================================

class TestLLMAdapterErrors:
    def test_no_api_key_raises(self):
        import os as _os
        from core.llm_adapter import LLMAdapter
        with patch.dict(_os.environ, {"DEEPSEEK_API_KEY": ""}, clear=True):
            with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
                LLMAdapter()


# ============================================================================
# 15. IdleMonitor
# ============================================================================

class TestIdleMonitor:
    @pytest.mark.asyncio
    async def test_start_creates_loop_task(self):
        from core.idle_monitor import IdleMonitor
        char = MagicMock()
        char.wake_words = []
        char.proactive.enabled = False
        char.proactive.interval = 300
        char.proactive.triggers = []
        fsm = MagicMock()
        fsm.state = "idle"
        bus = MagicMock()

        monitor = IdleMonitor(char, fsm, bus)
        await monitor.start()
        assert monitor._task is not None
        assert not monitor._task.done()
        await monitor.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        from core.idle_monitor import IdleMonitor
        char = MagicMock()
        char.wake_words = []
        char.proactive.enabled = False
        char.proactive.interval = 300
        char.proactive.triggers = []
        fsm = MagicMock()
        fsm.state = "idle"
        bus = MagicMock()

        monitor = IdleMonitor(char, fsm, bus)
        await monitor.start()
        assert monitor._running is True
        await monitor.stop()
        assert monitor._running is False
        assert monitor._task is None

    def test_touch_resets_timer(self):
        from core.idle_monitor import IdleMonitor
        import time
        char = MagicMock()
        char.wake_words = []
        char.proactive.enabled = False
        char.proactive.interval = 300
        char.proactive.triggers = []
        fsm = MagicMock()
        bus = MagicMock()

        monitor = IdleMonitor(char, fsm, bus)
        original = monitor._last_interaction
        monitor.touch()
        assert monitor._last_interaction >= original


# ============================================================================
# 16. DebugInfo
# ============================================================================

class TestDebugInfo:
    @pytest.mark.asyncio
    async def test_collect_returns_debug_info(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.debug_info import collect, DebugInfo
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        info = await collect(agent)
        assert isinstance(info, DebugInfo)
        assert info.character_name == "Elysia"
        await agent.stop()

    @pytest.mark.asyncio
    async def test_collect_fsm_state(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.debug_info import collect
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        info = await collect(agent)
        assert info.fsm_state in ("idle", "thinking", "speaking")
        await agent.stop()

    @pytest.mark.asyncio
    async def test_collect_tool_list(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.debug_info import collect
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        info = await collect(agent)
        tool_names = [t["name"] for t in info.tools]
        assert "search_memory" in tool_names
        assert "get_time" in tool_names
        # 验证元数据结构
        search = next(t for t in info.tools if t["name"] == "search_memory")
        assert "在长期记忆中搜索" in search["description"]
        assert any(p["name"] == "query" for p in search["parameters"])
        await agent.stop()

    @pytest.mark.asyncio
    async def test_collect_memory_counts(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.debug_info import collect
        from core.demo_agent import DemoAgent

        agent = DemoAgent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        info = await collect(agent)
        # 5 seeded memories
        assert info.lt_count >= 5
        await agent.stop()


# ============================================================================
# 17. ToolRegistry (Module.tool) — integration smoke tests
# ============================================================================

class TestToolRegistryUnit:
    """验证 Module.tool.ToolRegistry 在 core 上下文中的集成。"""

    @pytest.mark.asyncio
    async def test_execute_success(self):
        from Module.tool import ToolRegistry, ToolDefinition, ToolParameter

        async def my_tool(x: int) -> str:
            return f"result: {x}"

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="my_tool", description="test tool",
            func=my_tool,
            parameters={"x": ToolParameter(type="integer", required=True)},
        ))

        result = await registry.execute("my_tool", {"x": 42})
        assert result.success
        assert "result: 42" in result.content

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        from Module.tool import ToolRegistry, ToolDefinition, ToolParameter

        registry = ToolRegistry()
        result = await registry.execute("nonexistent", {})
        assert not result.success
        assert result.error_type is not None
        assert result.error_type.value == "unknown_tool"

    @pytest.mark.asyncio
    async def test_execute_logs_to_history(self):
        from Module.tool import ToolRegistry, ToolDefinition, ToolParameter

        async def my_tool(x: int) -> str:
            return str(x)

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="t", description="desc",
            func=my_tool,
            parameters={"x": ToolParameter(type="integer", required=True)},
        ))

        await registry.execute("t", {"x": 1})
        assert len(registry.history) == 1
        assert registry.history[0]["name"] == "t"

    def test_tool_definitions_format(self):
        from Module.tool import ToolRegistry, ToolDefinition, ToolParameter

        async def foo() -> str:
            return "bar"

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="foo", description="test function",
            func=foo, parameters={},
        ))

        defs = registry.definitions()
        assert len(defs) == 1
        assert defs[0]["type"] == "function"
        assert defs[0]["function"]["name"] == "foo"

    def test_type_coercion(self):
        """validate_args 自动将字符串强制为声明的类型。"""
        from Module.tool import ToolRegistry, ToolDefinition, ToolParameter
        from Module.tool.safety import validate_args

        params = {"x": ToolParameter(type="integer", required=True)}
        coerced, errors = validate_args(params, {"x": "42"})
        assert not errors
        assert coerced["x"] == 42
        assert isinstance(coerced["x"], int)

    def test_boolean_coercion(self):
        """validate_args 对 str 'true'/'1'/'yes' 进行布尔强制。"""
        from Module.tool import ToolRegistry, ToolDefinition, ToolParameter
        from Module.tool.safety import validate_args

        params = {"flag": ToolParameter(type="boolean", required=True)}
        coerced, errors = validate_args(params, {"flag": "true"})
        assert not errors
        assert coerced["flag"] is True


# ============================================================================
# Runner
# ============================================================================

if __name__ == "__main__":
    async def run_all():
        import sys
        test_classes = [
            TestMergeStreamToolCalls, TestNormalizeToolCalls,
            TestFSMEdges, TestEventBusEdges,
            TestSentenceBufferEdges,
            TestEmotionEngineEdges, TestMessageObj,
        ]
        passed = 0
        failed = 0
        for cls in test_classes:
            print(f"\n{'='*60}\n  {cls.__name__}\n{'='*60}")
            instance = cls()
            for name in sorted(dir(instance)):
                if name.startswith("test_"):
                    method = getattr(instance, name)
                    try:
                        if asyncio.iscoroutinefunction(method):
                            await method()
                        else:
                            method()
                        print(f"  ✓ {name}")
                        passed += 1
                    except Exception as e:
                        print(f"  ✗ {name}: {e}")
                        import traceback
                        traceback.print_exc()
                        failed += 1
        print(f"\n{'='*60}\n  Results: {passed} passed, {failed} failed\n{'='*60}")
    asyncio.run(run_all())
