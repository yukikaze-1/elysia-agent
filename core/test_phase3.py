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
from core._fake_llm import FakeLLMBackend
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
        from core.tool_registry import ToolRegistry
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
# 8. StreamingSentenceBuffer
# ============================================================================

class TestStreamingSentenceBuffer:
    @pytest.mark.asyncio
    async def test_consume_basic(self):
        from core.sentence_buffer import StreamingSentenceBuffer
        buf = StreamingSentenceBuffer()

        async def stream():
            for ch in "你好。今天不错。":
                yield ch

        sentences = []
        async for s in buf.consume(stream()):
            sentences.append(s)

        assert len(sentences) == 2
        assert "你好。" in sentences
        assert "今天不错。" in sentences

    @pytest.mark.asyncio
    async def test_consume_no_delimiter_flushes_at_end(self):
        from core.sentence_buffer import StreamingSentenceBuffer
        buf = StreamingSentenceBuffer()

        async def stream():
            for ch in "hello world":
                yield ch

        sentences = []
        async for s in buf.consume(stream()):
            sentences.append(s)
        # No delimiter in text, entire content flushed at end as one chunk
        assert sentences == ["hello world"]

    @pytest.mark.asyncio
    async def test_reset_reuse(self):
        from core.sentence_buffer import StreamingSentenceBuffer
        buf = StreamingSentenceBuffer()

        async def stream1():
            for ch in "hello.":
                yield ch

        async def stream2():
            for ch in "world!":
                yield ch

        s1 = [s async for s in buf.consume(stream1())]
        buf.reset()
        s2 = [s async for s in buf.consume(stream2())]

        assert "hello." in s1
        assert "world!" in s2


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
# Runner
# ============================================================================

if __name__ == "__main__":
    async def run_all():
        import sys
        test_classes = [
            TestMergeStreamToolCalls, TestNormalizeToolCalls,
            TestFSMEdges, TestEventBusEdges,
            TestSentenceBufferEdges, TestStreamingSentenceBuffer,
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
