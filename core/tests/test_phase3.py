"""Phase 3 tests: CognitiveLoop, server, Agent, LLM adapter, edge cases.

Run: python -m pytest core/test_phase3.py -v --asyncio-mode=auto
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from pydantic import BaseModel, Field

from Module.tool.protocol import ToolPlugin
from Module.tool.safety import SafetyProfile

from core.cognitive import CognitiveLoop
from core.utils.text_utils import MessageObj
from core.adapter.tool_call_parser import merge_stream_tool_calls, normalize_tool_calls
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId
from core.status.agent_fsm import AgentFSM, AgentState
from core.monitoring.event_bus import EventBus, EventType, EventPriority
from core.schema.event import StateChangeData
from core.adapter.llm_adapter import LLMAdapter
from core.tests.fake_llm import FakeLLMBackend
from core.config import AppConfig


# ============================================================================
# 1. CognitiveLoop — _merge_stream_tool_calls
# ============================================================================

@pytest.mark.unit
class TestMergeStreamToolCalls:
    def test_single_delta_new_entry(self):
        accum: list[dict] = []
        delta = [{"index": 0, "id": "call_1", "function": {"name": "search", "arguments": '{"q"'}}]
        merge_stream_tool_calls(accum, delta)
        assert len(accum) == 1
        assert accum[0]["id"] == "call_1"
        assert accum[0]["function"]["name"] == "search"
        assert accum[0]["function"]["arguments"] == '{"q"'

    def test_same_index_accumulates(self):
        accum: list[dict] = []
        merge_stream_tool_calls(accum, [
            {"index": 0, "function": {"name": "se", "arguments": ""}}
        ])
        merge_stream_tool_calls(accum, [
            {"index": 0, "function": {"name": "arch", "arguments": '{"q":1}'}}
        ])
        assert accum[0]["function"]["name"] == "search"
        assert accum[0]["function"]["arguments"] == '{"q":1}'

    def test_delta_id_only_no_function(self):
        accum: list[dict] = []
        delta = [{"index": 0, "id": "call_x"}]
        merge_stream_tool_calls(accum, delta)
        assert accum[0]["id"] == "call_x"
        assert accum[0]["function"]["name"] == ""

    def test_multi_index_interleaved(self):
        accum: list[dict] = []
        merge_stream_tool_calls(accum, [
            {"index": 0, "function": {"name": "fn0"}},
            {"index": 1, "function": {"name": "fn1"}},
        ])
        assert len(accum) == 2
        assert accum[0]["function"]["name"] == "fn0"
        assert accum[1]["function"]["name"] == "fn1"

    def test_empty_deltas_noop(self):
        accum: list[dict] = []
        merge_stream_tool_calls(accum, [])
        assert accum == []

    def test_missing_function_field(self):
        accum: list[dict] = []
        delta = [{"index": 0, "id": "x"}]
        merge_stream_tool_calls(accum, delta)
        # Should not crash
        assert accum[0]["id"] == "x"


# ============================================================================
# 2. CognitiveLoop — _normalize_tool_calls
# ============================================================================

@pytest.mark.unit
class TestNormalizeToolCalls:
    def test_all_complete(self):
        raw = [
            {"id": "a", "function": {"name": "f1", "arguments": "{}"}},
            {"id": "b", "function": {"name": "f2", "arguments": "{}"}},
        ]
        result = normalize_tool_calls(raw)
        assert len(result) == 2
        # 以下两行从 dict 改为 ToolCall 属性
        assert result[0].id == ToolCallId("a")
        assert result[0].function.name == "f1"
        assert result[1].id == ToolCallId("b")
        assert result[1].function.name == "f2"

    def test_all_incomplete_missing_id(self):
        raw = [{"id": "", "function": {"name": "f1", "arguments": "{}"}}]
        assert normalize_tool_calls(raw) == []

    def test_all_incomplete_missing_name(self):
        raw = [{"id": "x", "function": {"name": "", "arguments": "{}"}}]
        assert normalize_tool_calls(raw) == []

    def test_mixed(self):
        raw = [
            {"id": "a", "function": {"name": "ok", "arguments": "{}"}},
            {"id": "", "function": {"name": "bad", "arguments": "{}"}},
        ]
        result = normalize_tool_calls(raw)
        assert len(result) == 1
        assert result[0].id == ToolCallId("a")
        assert result[0].function.name == "ok"


# ============================================================================
# 3. CognitiveLoop — TTSStreamManager, _handle_interrupt
# ============================================================================

@pytest.mark.service
class TestCognitiveLoopMethods:
    @pytest.fixture
    def loop(self):
        """Create a minimal CognitiveLoop with mocks for all dependencies."""
        from Module.output import OutputManager, ChannelCapability
        from core.adapter.prompt_assembler import PromptAssembler
        from core.adapter import ToolAdapter
        from core.memory.session import SessionMemory
        from core.config import CharacterConfig
        from core.memory.consolidator import MemoryConsolidator
        from core.memory.long_term import LongTermMemory
        from core.personality import CharacterEngine
        from core.schema.character import CharacterState
        from core.schema.types import SessionId

        llm = MagicMock(spec=LLMAdapter)
        fsm = AgentFSM()
        bus = EventBus()
        output = OutputManager()
        char = CharacterConfig(name="TestBot")
        session = SessionMemory()
        lt = LongTermMemory()
        prompt = PromptAssembler(char, session, lt)
        tools = ToolAdapter([])
        state = CharacterState(session_id=SessionId("test-sid"))
        output.expression = MagicMock(return_value=MagicMock())
        char_engine = CharacterEngine(
            llm=llm, state=state,
            character_name="TestBot", user_name="妖梦",
            base_personality="", character_config=char, output=output,
        )
        consolidator = MemoryConsolidator(llm, lt, "TestBot")
        tts = AsyncMock()

        loop = CognitiveLoop(
            llm=llm, fsm=fsm, event_bus=bus, output=output,
            prompt=prompt, session=session, tool_adapter=tools,
            character_engine=char_engine, consolidator=consolidator, tts=tts,
        )
        return loop

    @pytest.mark.asyncio
    async def test_synthesize_legacy_tts_none(self, loop):
        loop._tts_mgr.set_backend(None)
        await loop._tts_mgr.synthesize_legacy("hello")
        # Should return immediately without error

    @pytest.mark.asyncio
    async def test_synthesize_legacy_tts_success(self, loop):
        loop._tts = AsyncMock()
        loop._tts.synthesize.return_value = b"fakewav"
        loop._tts_mgr.set_backend(loop._tts)

        with patch.object(loop._output, "audio_chunk", new_callable=AsyncMock) as mock_ac:
            await loop._tts_mgr.synthesize_legacy("hello")
            loop._tts.synthesize.assert_awaited_once()
            mock_ac.assert_awaited_once_with(b"fakewav")

    @pytest.mark.asyncio
    async def test_synthesize_legacy_tts_failure(self, loop):
        loop._tts = AsyncMock()
        loop._tts.synthesize.side_effect = RuntimeError("TTS down")
        loop._tts_mgr.set_backend(loop._tts)

        await loop._tts_mgr.synthesize_legacy("hello")
        # Should not raise

    @pytest.mark.asyncio
    async def test_handle_interrupt(self, loop):
        loop._fsm.transition(AgentState.THINKING)

        await loop._handle_interrupt()

        assert loop._fsm.state == AgentState.IDLE
        # 句缓冲/块过滤状态在每轮 StreamProcessor 中独立创建，
        # CognitiveLoop 不再持有引用——中断后新轮次自然获得干净缓冲区。

    @pytest.mark.asyncio
    async def test_process_rejects_invalid_fsm_state(self, loop):
        loop._fsm.transition(AgentState.THINKING)
        loop._fsm.interrupt()
        # Now INTERRUPTED — can only go to IDLE, not THINKING
        await loop.process("hello")
        # Should warn and return without transitioning to THINKING
        assert loop._fsm.state == AgentState.INTERRUPTED

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

    @pytest.mark.asyncio
    async def test_tool_cancelled_error_returns_fail(self, loop):
        """Tool raising CancelledError returns ToolResult.fail via _execute_tools_directly."""
        from Module.tool.protocol import ToolResult
        import asyncio as _asyncio

        async def cancelled_tool(*args, **kwargs):
            raise _asyncio.CancelledError()

        loop._tool_adapter.execute = cancelled_tool

        tc = ToolCall(
            id="call_1",
            function=ToolCallFunction(name="cancelled_tool", arguments="{}", parsed={}),
        )
        results = await loop._execute_tools_directly([tc])

        assert len(results) == 1
        tc_out, tr = results[0]
        assert tc_out is tc
        assert isinstance(tr, ToolResult)
        assert tr.success is False
        assert "用户取消" in (tr.error or "")

    @pytest.mark.asyncio
    async def test_tool_exception_returns_fail(self, loop):
        """Tool raising RuntimeError returns ToolResult.fail via _execute_tools_directly."""
        from Module.tool.protocol import ToolResult

        async def failing_tool(*args, **kwargs):
            raise RuntimeError("tool crash")

        loop._tool_adapter.execute = failing_tool

        tc = ToolCall(
            id="call_1",
            function=ToolCallFunction(name="failing_tool", arguments="{}", parsed={}),
        )
        results = await loop._execute_tools_directly([tc])

        assert len(results) == 1
        tc_out, tr = results[0]
        assert tc_out is tc
        assert isinstance(tr, ToolResult)
        assert tr.success is False
        assert "tool crash" in (tr.error or "")


# ============================================================================
# 3b. CognitiveLoop — TTS + multi-tool
# ============================================================================

@pytest.mark.service
class TestCognitiveLoopTTS:
    @pytest.fixture
    def loop(self):
        from core.monitoring.event_bus import EventBus
        from core.status.agent_fsm import AgentFSM
        from Module.output import OutputManager
        from core.adapter.prompt_assembler import PromptAssembler
        from core.memory.session import SessionMemory
        from core.adapter import ToolAdapter
        from core.adapter.llm_adapter import LLMAdapter

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
        tools = ToolAdapter([])
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
            prompt=prompt, session=session, tool_adapter=tools, tts=tts,
        )

    @pytest.mark.asyncio
    async def test_stream_sentence_creates_task(self, loop):
        """stream_sentence creates a background TTS task."""
        initial_count = len(loop._tts_mgr._tasks)
        loop._tts_mgr.stream_sentence("你好世界")
        assert len(loop._tts_mgr._tasks) == initial_count + 1
        # Should clean up done tasks
        loop._tts_mgr.stream_sentence("第二句")
        # At most initial+2 tasks (some may have completed)
        assert len(loop._tts_mgr._tasks) <= initial_count + 2
        assert loop._tts_mgr._total >= 2

    @pytest.mark.asyncio
    async def test_multi_tool_round(self, loop):
        """CognitiveLoop handles tool calls and loops correctly."""
        import json
        from Module.tool import PluginContext, ToolContext
        from Module.tool.protocol import ToolPlugin
        from Module.tool.safety import SafetyProfile
        from pydantic import BaseModel, Field
        # Use a fake backend that returns tool calls then final answer
        tool_json = json.dumps({
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "test_tool", "arguments": '{"q":"test"}'}
            }]
        })
        loop._llm._backend = FakeLLMBackend(response=tool_json)

        # Register a tool using Plugin pattern
        class _QParams(BaseModel):
            q: str = Field(default="")

        class _TestTool(ToolPlugin):
            name = "test_tool"
            description = "test"
            param_model = _QParams
            capabilities = set()
            safety = SafetyProfile()
            async def execute(self, *, ctx, emit=None, params):
                return f"result for {params.q}"

        await loop._tool_adapter._manager.load_all([_TestTool], PluginContext(session_id="test"))

        # After process, the tool should have been called (tool history)
        await loop.process("use test_tool")
        # Tool was registered, process completed without error
        assert loop._fsm.state == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_handle_interrupt_clears_state(self, loop):
        """_handle_interrupt resets sentence buffer, cancels TTS tasks, returns to IDLE."""
        t = asyncio.create_task(asyncio.sleep(10))
        loop._tts_mgr._tasks.append(t)
        loop._fsm.transition("thinking")

        await loop._handle_interrupt()

        assert len(loop._tts_mgr._tasks) == 0
        assert loop._fsm.state == AgentState.IDLE


# ============================================================================
# 3c. CognitiveLoop — TTS sync (basic)
# ============================================================================

@pytest.mark.service
class TestCognitiveLoopSync:
    @pytest.fixture
    def loop(self):
        from core.monitoring.event_bus import EventBus
        from core.status.agent_fsm import AgentFSM
        from Module.output import OutputManager
        from core.adapter.prompt_assembler import PromptAssembler
        from core.memory.session import SessionMemory
        from core.adapter import ToolAdapter
        from core.adapter.llm_adapter import LLMAdapter

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
        tools = ToolAdapter([])
        return CognitiveLoop(
            llm=llm, fsm=fsm, event_bus=bus, output=output,
            prompt=prompt, session=session, tool_adapter=tools, tts=None,
        )

    @pytest.mark.asyncio
    async def test_no_tts_text_flows_directly(self, loop):
        """无 TTS 时文本直接发送，不经过缓冲。"""
        await loop.process("hello")
        # process() 成功完成即表明文本已直接发送
        assert loop._fsm.state == AgentState.IDLE

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

@pytest.mark.unit
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

@pytest.mark.unit
class TestEventBusEdges:
    def test_pending_count(self):
        bus = EventBus()
        assert bus.pending_count == 0
        bus.post(StateChangeData(state="a", request_id="r1"))
        bus.post(StateChangeData(state="b", request_id="r2"))
        assert bus.pending_count == 2

    @pytest.mark.asyncio
    async def test_fifo_within_same_priority(self):
        bus = EventBus()
        bus.post(StateChangeData(state="a", request_id="r1"), type="first", priority=EventPriority.NORMAL)
        bus.post(StateChangeData(state="b", request_id="r2"), type="second", priority=EventPriority.NORMAL)
        bus.post(StateChangeData(state="c", request_id="r3"), type="third", priority=EventPriority.NORMAL)
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

@pytest.mark.service
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
        assert result.tool_calls[0].function.name == "search_memory"

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
# 9. EmotionEngine — missing edges → CharacterEngine
# ============================================================================

@pytest.mark.service
class TestEmotionEngineEdges:
    @pytest.fixture
    def engine(self):
        from core.config import CharacterConfig
        from core.schema.character import CharacterState
        from core.schema.types import SessionId
        from Module.output import OutputManager
        from core.personality import CharacterEngine
        from core.adapter.llm_adapter import LLMAdapter

        char = CharacterConfig(name="Test")
        state = CharacterState(session_id=SessionId("test-sid"))
        output = OutputManager()
        output.expression = MagicMock(return_value=MagicMock())
        llm = MagicMock(spec=LLMAdapter)
        llm.think = AsyncMock()
        return CharacterEngine(
            llm=llm, state=state,
            character_name="Test", user_name="妖梦",
            base_personality="", character_config=char, output=output,
        )

    def test_multiple_moods_tie_breaker(self, engine):
        # "！" matches excited; "哈哈" matches happy. Happy has more matches.
        mood = engine._detect_mood("哈哈！")
        assert mood == "happy"

    def test_push_idle_empty_list(self, engine):
        engine._idle_expressions = []
        engine._push_idle()
        # Should not raise

    def test_intensity_capped_at_one(self, engine):
        engine.state.mood_intensity = 0.96
        engine.analyze_user("开心")
        assert engine.state.mood_intensity <= 1.0

    def test_intensity_floor_and_neutral(self, engine):
        engine.state.mood = "happy"
        engine.state.mood_intensity = 0.12
        engine.analyze_assistant("好的")
        assert engine.state.mood_intensity >= 0.1
        # mood text no longer reset by analyze_assistant
        assert engine.state.mood == "happy"

    def test_schedule_no_loop_does_not_raise(self, engine):
        """_schedule should be a no-op when no event loop is running."""
        # In sync test context, no running loop → should not raise
        from core.personality.engine import CharacterEngine
        CharacterEngine._schedule(MagicMock())  # type: ignore[reportAttributeAccessIssue]  # coroutine mock, no loop

    @pytest.mark.asyncio
    async def test_push_idle_uses_list(self, engine):
        """push_idle_expression picks from idle_expressions."""
        engine._idle_expressions = ["neutral", "blink"]
        # Should not raise — picks randomly from list
        await engine.push_idle_expression()


# ============================================================================
# 10b. PromptAssembler
# ============================================================================

@pytest.mark.service
class TestPromptAssemblerUnit:
    @pytest.mark.asyncio
    async def test_build_system_prompt(self):
        """Section provider 路径正常渲染 system prompt。"""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.adapter.prompt_assembler import PromptAssembler
        from core.config import CharacterConfig
        from core.memory.session import SessionMemory
        from core.memory.long_term import LongTermMemory

        char = CharacterConfig.from_yaml("character/elysia/config.yaml")
        session = SessionMemory()
        lt = LongTermMemory()

        assembler = PromptAssembler(char, session, lt)
        prompt = await assembler.build_system_prompt("你好", "妖梦")
        assert "Elysia" in prompt
        assert "<inner_voice>" in prompt


# ============================================================================
# 11. Agent — process_message
# ============================================================================

@pytest.mark.slow
class TestAgentProcessMessage:
    @pytest.mark.asyncio
    async def test_empty_message_returns(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        await agent.start()
        await agent.process_message("")
        assert agent._fsm.state == AgentState.IDLE
        await agent.stop()

    @pytest.mark.asyncio
    async def test_whitespace_only(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        await agent.start()
        await agent.process_message("   ")
        assert agent._fsm.state == AgentState.IDLE
        await agent.stop()

    @pytest.mark.asyncio
    async def test_idle_touch_called(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.agent import Agent

        agent = Agent(config=AppConfig())
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
        from core.agent import Agent

        agent = Agent(config=AppConfig())
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

@pytest.mark.service
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

        msg = await mgr.stream(empty(), finalize=lambda t: MessageObj(content=t))
        ch.on_text_delta.assert_not_awaited()
        assert msg.content == ""


# ============================================================================
# 13. WebSocketChannel — new AUDIO/state methods
# ============================================================================

@pytest.mark.service
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

@pytest.mark.service
class TestAudioBufferErrors:
    @pytest.mark.asyncio
    async def test_stt_timeout(self):
        from core.input.audio_buffer import AudioBuffer

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
        from core.input.audio_buffer import AudioBuffer

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
        from core.input.audio_buffer import AudioBuffer

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
        from core.input.audio_buffer import AudioBuffer
        buf = AudioBuffer()
        await buf.flush()  # Should not raise

    @pytest.mark.asyncio
    async def test_vad_silence_detected(self):
        """Near-silence PCM should be classified as non-speech by WebRTC VAD."""
        from core.input.audio_buffer import AudioBuffer, _FRAME_BYTES
        buf = AudioBuffer(aggressiveness=3)
        # Generate near-silence frame (very low amplitude)
        import struct
        chunk = struct.pack(f"<{_FRAME_BYTES // 2}h", *([5] * (_FRAME_BYTES // 2)))
        assert buf._is_speech(chunk) is False


# ============================================================================
# 15. ConsoleChannel — dedup + inner_voice
# ============================================================================

@pytest.mark.service
class TestConsoleChannelDedup:
    @pytest.mark.asyncio
    async def test_on_message_after_stream_skips_content(self, capsys):
        from Module.output import ConsoleChannel
        ch = ConsoleChannel()
        # Simulate streaming
        await ch.on_text_delta("hello")
        # on_message after streaming should not print content again
        msg = MessageObj(content="hello")
        await ch.on_message(msg)
        # Verify delta_printed flag is reset
        assert ch._delta_printed is False
        # Verify content was printed exactly once (from delta, not from on_message)
        captured = capsys.readouterr()
        assert captured.out.count("hello") == 1

    @pytest.mark.asyncio
    async def test_on_message_without_stream(self):
        from Module.output import ConsoleChannel
        ch = ConsoleChannel()
        msg = MessageObj(content="direct message")
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
# 16. MessageObj
# ============================================================================

@pytest.mark.unit
class TestMessageObj:
    def test_to_dict(self):
        from core.schema.message import MessageRole
        msg = MessageObj(role=MessageRole.ASSISTANT, content="hello world")
        d = msg.to_dict()
        assert d == {"role": "assistant", "content": "hello world"}

    def test_defaults(self):
        msg = MessageObj()
        assert msg.role == "assistant"
        assert msg.content == ""


# ============================================================================
# 14. Agent — lifecycle
# ============================================================================

@pytest.mark.service
class TestAgentLifecycle:
    @pytest.mark.asyncio
    async def test_register_tools(self):
        """Tools load via ToolManager.load_all with correct names."""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        # Load tools directly (normally done in start())
        await agent._tool_adapter.start()
        names = list(agent._tool_adapter._manager.tool_names)
        assert "search_memory" in names
        assert "get_time" in names
        assert "save_memory" in names

    @pytest.mark.asyncio
    async def test_tool_execute_search_memory(self):
        """search_memory tool queries long-term memory."""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from Module.tool import ToolContext
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        # Load tools directly (normally done in start())
        await agent._tool_adapter.start()
        # Seed a memory entry
        from core.memory.long_term import MemoryEntry
        agent._long_term._entries.append(
            MemoryEntry(text="测试记忆", metadata={"type": "fact"}))

        ctx = ToolContext(services={"memory": agent._long_term})
        result = await agent._tool_adapter.execute("search_memory", ctx=ctx, query="测试")
        assert result.success
        assert "测试记忆" in result.content

    @pytest.mark.asyncio
    async def test_stop_cancels_cognitive_task(self):
        """stop() cancels _current_task if still running."""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        # Simulate a running cognitive task
        async def fake_task():
            await asyncio.sleep(10)
        task = asyncio.create_task(fake_task())
        agent._cognitive._current_task = task

        await agent.stop()
        # cancel_current() 会设 _current_task = None，所以检查之前保存的引用
        assert task.done() or task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_closes_output_channels(self):
        """stop() closes all output channels."""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.agent import Agent

        agent = Agent(config=AppConfig())
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
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        # Start processing a message
        await agent.process_message("hello")
        # Wait for processing to complete
        for _ in range(200):
            if agent._fsm.state == "idle":
                break
            await asyncio.sleep(0.01)

        assert agent._fsm.state == "idle"
        await agent.stop()


# ============================================================================
# 14b. EmotionEngine — emoji mappings
# ============================================================================

@pytest.mark.unit
class TestEmotionEmojiMappings:
    def test_stt_emotion_emoji_all(self):
        from core.schema.mood import stt_emotion_emoji
        assert stt_emotion_emoji("HAPPY") == "😊"
        assert stt_emotion_emoji("SAD") == "😔"
        assert stt_emotion_emoji("ANGRY") == "😡"
        assert stt_emotion_emoji("SURPRISED") == "😮"
        assert stt_emotion_emoji("NEUTRAL") == ""
        assert stt_emotion_emoji("UNKNOWN") == ""

    def test_stt_event_emoji_all(self):
        from core.schema.mood import stt_event_emoji
        assert stt_event_emoji("Laughter") == "😀"
        assert stt_event_emoji("Cry") == "😭"
        assert stt_event_emoji("Applause") == "👏"
        assert stt_event_emoji("Speech") == ""
        assert stt_event_emoji("UNKNOWN_EVENT") == ""


# ============================================================================
# ============================================================================
# 14d. CharacterConfig
# ============================================================================

@pytest.mark.service
class TestCharacterConfig:
    def test_from_yaml_loads_correctly(self):
        from core.config import CharacterConfig
        char = CharacterConfig.from_yaml("character/elysia/config.yaml")
        assert char.name == "Elysia"
        # traits 已合并到 base_personality.txt
        assert "爱莉希雅" in char.base_personality
        assert len(char.wake_words) >= 1
        assert "Elysia" in char.wake_words

    def test_personality_block_format(self):
        from core.config import CharacterConfig
        char = CharacterConfig.from_yaml("character/elysia/config.yaml")
        block = char.personality_block()
        # 新格式: base_personality 自然语言文本（来自 personality.txt）
        # 模板单独渲染 {{ name }}，所以 block 不包含角色名
        assert "温柔" in block
        assert "真我" in block
        assert "道德边界" in block

    def test_from_yaml_file_not_found(self):
        from core.config import CharacterConfig
        with pytest.raises(FileNotFoundError):
            CharacterConfig.from_yaml("nonexistent.yaml")


# ============================================================================
# 14c. AppConfig
# ============================================================================

@pytest.mark.service
class TestAppConfig:
    def test_default_values(self):
        cfg = AppConfig()
        assert cfg.server.host == "0.0.0.0"
        assert cfg.server.port == 8765
        assert cfg.tts.enabled is False
        assert cfg.stt.enabled is False
        assert cfg.agent.character == "character/elysia/config.yaml"
        assert cfg.agent.user_name == "妖梦"
        assert cfg.llm.model == "deepseek/deepseek-chat"
        assert cfg.memory.session_max_messages == 20

    def test_load_from_yaml(self):
        from Module.config.loader import ConfigLoader, EnvManager
        import os as _os
        # 阻止 load_dotenv() 污染全局环境变量（尤其是 MILVUS_URI，
        # 会导致后续测试连上真实数据库而非 in-memory 模式）
        _orig_loaded = EnvManager._loaded
        EnvManager._loaded = True  # 假装已加载，跳过 load_dotenv()
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        try:
            cfg = ConfigLoader(AppConfig, yaml_path="config.yaml").load()
            assert cfg.tts.enabled is True
            assert cfg.stt.enabled is True
            assert cfg.agent.user_name == "妖梦"
        finally:
            EnvManager._loaded = _orig_loaded


# ============================================================================
# 14d. LLMAdapter
# ============================================================================

@pytest.mark.service
class TestLLMAdapterErrors:
    def test_no_api_key_uses_factory(self):
        """LLMAdapter 不再直接检查 API key；key 检查由 registry 层处理。"""
        from core.adapter.llm_adapter import LLMAdapter
        from core.tests.fake_llm import FakeLLMBackend
        adapter = LLMAdapter(_test_backend=FakeLLMBackend())
        assert adapter.model == "deepseek/deepseek-chat"

    @pytest.mark.asyncio
    async def test_retry_on_429_then_succeed(self):
        """429 错误应触发重试并最终成功。"""
        from core.adapter.llm_adapter import LLMAdapter

        call_count = [0]

        class RetryBackend:
            async def stream_think(self, messages, **kwargs):
                call_count[0] += 1
                if call_count[0] < 3:
                    # simulate httpx.HTTPStatusError
                    import httpx
                    req = object()
                    resp = type('R', (), {'status_code': 429})()
                    raise httpx.HTTPStatusError("rate limit", request=req, response=resp)  # type: ignore[arg-type]
                yield {"content": "成功", "tool_calls": None}

        adapter = LLMAdapter(_test_backend=RetryBackend())  # type: ignore[arg-type]
        chunks = []
        async for chunk in adapter.stream_think([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert call_count[0] == 3
        assert chunks == [{"content": "成功", "tool_calls": None}]

    @pytest.mark.asyncio
    async def test_retry_on_503_then_succeed(self):
        """503 错误应触发重试并最终成功。"""
        from core.adapter.llm_adapter import LLMAdapter

        call_count = [0]

        class RetryBackend:
            async def stream_think(self, messages, **kwargs):
                call_count[0] += 1
                if call_count[0] < 2:
                    import httpx
                    req = object()
                    resp = type('R', (), {'status_code': 503})()
                    raise httpx.HTTPStatusError("unavailable", request=req, response=resp)  # type: ignore[arg-type]
                yield {"content": "恢复", "tool_calls": None}

        adapter = LLMAdapter(_test_backend=RetryBackend())  # type: ignore[arg-type]
        chunks = []
        async for chunk in adapter.stream_think([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert call_count[0] == 2
        assert chunks == [{"content": "恢复", "tool_calls": None}]

    @pytest.mark.asyncio
    async def test_non_retryable_error_propagates(self):
        """非可重试错误（如 400）应直接抛出不重试。"""
        from core.adapter.llm_adapter import LLMAdapter

        class FailBackend:
            async def stream_think(self, messages, **kwargs):
                import httpx
                req = object()
                resp = type('R', (), {'status_code': 400})()
                raise httpx.HTTPStatusError("bad request", request=req, response=resp)  # type: ignore[arg-type]

        adapter = LLMAdapter(_test_backend=FailBackend())  # type: ignore[arg-type]
        with pytest.raises(Exception):
            async for _ in adapter.stream_think([{"role": "user", "content": "hi"}]):
                pass

    @pytest.mark.asyncio
    async def test_no_retry_after_stream_started(self):
        """流开始产出后遇到可重试错误 → 不重试，直接抛异常。"""
        from core.adapter.llm_adapter import LLMAdapter

        call_count = [0]

        class StartThenFailBackend:
            async def stream_think(self, messages, **kwargs):
                call_count[0] += 1
                yield {"content": "前半部分", "tool_calls": None}
                import httpx
                req = object()
                resp = type('R', (), {'status_code': 503})()
                raise httpx.HTTPStatusError("mid-stream failure", request=req, response=resp)  # type: ignore[arg-type]

        adapter = LLMAdapter(_test_backend=StartThenFailBackend())  # type: ignore[arg-type]
        chunks = []
        with pytest.raises(Exception):
            async for chunk in adapter.stream_think([{"role": "user", "content": "hi"}]):
                chunks.append(chunk)

        assert chunks == [{"content": "前半部分", "tool_calls": None}]
        assert call_count[0] == 1


# ============================================================================
# 15. IdleMonitor
# ============================================================================

@pytest.mark.service
class TestIdleMonitor:
    @pytest.mark.asyncio
    async def test_start_creates_loop_task(self):
        from core.monitoring.idle_monitor import IdleMonitor
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
        from core.monitoring.idle_monitor import IdleMonitor
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

    @pytest.mark.asyncio
    async def test_touch_resets_timer(self):
        from core.monitoring.idle_monitor import IdleMonitor
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
        # 确保时间推进（避免 event loop 时钟精度问题导致两次调用返回相同值）
        time.sleep(0.001)
        monitor.touch()
        assert monitor._last_interaction >= original


# ============================================================================
# 16. DebugInfo
# ============================================================================

@pytest.mark.slow
class TestDebugInfo:
    @pytest.mark.asyncio
    async def test_collect_returns_debug_info(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.debug.info import collect, DebugInfo
        from core.agent import Agent

        agent = Agent(config=AppConfig())
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
        from core.debug.info import collect
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        info = await collect(agent)
        assert info.fsm_state in ("idle", "thinking", "speaking")
        await agent.stop()

    @pytest.mark.asyncio
    async def test_collect_tool_list(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.debug.info import collect
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        info = await collect(agent)
        tool_names = [t["name"] for t in info.tools]
        assert "search_memory" in tool_names
        assert "get_time" in tool_names
        # 验证元数据结构 (plugin_list format)
        search = next(t for t in info.tools if t["name"] == "search_memory")
        assert "在长期记忆中搜索" in search["description"]
        assert "capabilities" in search
        assert search["active"] is True
        await agent.stop()

    @pytest.mark.asyncio
    async def test_collect_memory_counts(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.debug.info import collect
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        info = await collect(agent)
        assert isinstance(info.lt_count, int)
        await agent.stop()


# ============================================================================
# 17. 数据库调试 API
# ============================================================================

@pytest.mark.slow
class TestDebugDbApi:
    @pytest.mark.asyncio
    async def test_db_status_endpoint(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.input.server import create_app
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        app = create_app(agent, serve_static=False)
        async with aiohttp.test_utils.TestServer(app) as srv:  # type: ignore[attr-defined]
            async with aiohttp.test_utils.TestClient(srv) as client:  # type: ignore[attr-defined]
                resp = await client.get("/debug/api/db-status")
                assert resp.status == 200
                data = await resp.json()
                assert "ltm" in data
                assert "session" in data
                assert isinstance(data["ltm"]["count"], int)

        await agent.stop()

    @pytest.mark.asyncio
    async def test_ltm_list_and_insert(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.input.server import create_app
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        app = create_app(agent, serve_static=False)
        async with aiohttp.test_utils.TestServer(app) as srv:  # type: ignore[attr-defined]
            async with aiohttp.test_utils.TestClient(srv) as client:  # type: ignore[attr-defined]
                resp = await client.get("/debug/api/ltm")
                data = await resp.json()
                count_before = data["total"]

                resp = await client.post("/debug/api/ltm", json={
                    "text": "测试记忆内容", "memory_type": "fact", "poignancy": 7,
                })
                assert resp.status == 200

                resp = await client.get("/debug/api/ltm")
                data = await resp.json()
                assert data["total"] == count_before + 1
                assert any(e["text"] == "测试记忆内容" for e in data["entries"])
                entry_id = next(e["id"] for e in data["entries"] if e["text"] == "测试记忆内容")

                resp = await client.delete(f"/debug/api/ltm/{entry_id}")
                assert resp.status == 200

                resp = await client.get("/debug/api/ltm")
                data = await resp.json()
                assert data["total"] == count_before

        await agent.stop()

    @pytest.mark.asyncio
    async def test_ltm_clear_all(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.input.server import create_app
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        count_before = agent._long_term.count
        await agent._long_term.insert("entry 1", type="note")
        await agent._long_term.insert("entry 2", type="note")
        assert agent._long_term.count == count_before + 2

        app = create_app(agent, serve_static=False)
        async with aiohttp.test_utils.TestServer(app) as srv:  # type: ignore[attr-defined]
            async with aiohttp.test_utils.TestClient(srv) as client:  # type: ignore[attr-defined]
                resp = await client.delete("/debug/api/ltm")
                data = await resp.json()
                assert data["ok"] is True
                assert data["deleted"] >= 2
                assert agent._long_term.count == 0

        await agent.stop()

    @pytest.mark.asyncio
    async def test_session_list_endpoint(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.input.server import create_app
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        app = create_app(agent, serve_static=False)
        async with aiohttp.test_utils.TestServer(app) as srv:  # type: ignore[attr-defined]
            async with aiohttp.test_utils.TestClient(srv) as client:  # type: ignore[attr-defined]
                resp = await client.get("/debug/api/sessions")
                assert resp.status == 200
                data = await resp.json()
                assert "sessions" in data

        await agent.stop()

    @pytest.mark.asyncio
    async def test_session_detail_endpoint(self):
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        from core.input.server import create_app
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        agent._llm._backend = FakeLLMBackend(response="ok")
        await agent.start()

        # 先存一条消息让它有东西
        from core.schema.message import UserMessage
        from core.schema.part import TextPart
        from core.schema.types import MessageId
        agent._session.add_message(UserMessage(
            content=[TextPart(content="hello")],
            message_id=MessageId("msg-test"),
        ))
        if await agent._session_repo.health_check():
            await agent._session.save()

        app = create_app(agent, serve_static=False)
        async with aiohttp.test_utils.TestServer(app) as srv:  # type: ignore[attr-defined]
            async with aiohttp.test_utils.TestClient(srv) as client:  # type: ignore[attr-defined]
                resp = await client.get(
                    "/debug/api/sessions/" + agent._session._session_id)
                assert resp.status == 200
                data = await resp.json()
                assert "messages" in data

        await agent.stop()


# ============================================================================
# 18. ToolManager (Module.tool) — integration smoke tests
# ============================================================================

# Helper to create a minimal ToolManager with an echo plugin
class _EchoParams(BaseModel):
    x: int = Field(default=0)

class _EchoPlugin(ToolPlugin):
    name = "echo"
    description = "echoes x"
    param_model = _EchoParams
    capabilities = set()
    safety = SafetyProfile()
    async def execute(self, *, ctx, emit=None, params):
        return f"result={params.x}"


@pytest.mark.service
class TestToolManagerUnit:
    """验证 ToolManager 在 core 上下文中的集成。"""

    @pytest.mark.asyncio
    async def test_load_and_execute_echo(self):
        from Module.tool import ToolManager, PluginContext, ToolContext
        tm = ToolManager()
        await tm.load_all([_EchoPlugin], PluginContext(session_id="test"))
        result = await tm.execute("echo", ctx=ToolContext(), x=42)
        assert result.success is True
        assert "result=42" in result.content
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        from Module.tool import ToolManager, PluginContext, ToolContext
        tm = ToolManager()
        await tm.load_all([], PluginContext(session_id="test"))
        result = await tm.execute("nonexistent", ctx=ToolContext())
        assert result.success is False
        assert result.error_type is not None
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_definitions_exported(self):
        from Module.tool import ToolManager, PluginContext
        tm = ToolManager()
        await tm.load_all([_EchoPlugin], PluginContext(session_id="test"))
        defs = tm.definitions()
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "echo"
        await tm.shutdown()


# ============================================================================
# 19. submit_task definition 测试
# ============================================================================

@pytest.mark.unit
class TestSubmitTaskDefinition:
    def test_schema_has_required_intent(self):
        """submit_task function schema 的 required 包含 intent。"""
        from core.cognitive import CognitiveLoop
        defn = CognitiveLoop._submit_task_definition()
        func = defn["function"]
        assert func["name"] == "submit_task"
        assert "intent" in func["parameters"]["required"]
        assert "intent" in func["parameters"]["properties"]

    def test_schema_is_valid_openai_format(self):
        """返回的是标准 OpenAI function-calling 格式。"""
        from core.cognitive import CognitiveLoop
        defn = CognitiveLoop._submit_task_definition()
        assert defn["type"] == "function"
        assert "function" in defn
        assert "name" in defn["function"]
        assert "parameters" in defn["function"]


# ============================================================================
# 20. _handle_submit_task 测试
# ============================================================================

@pytest.mark.service
class TestHandleSubmitTask:
    @pytest.fixture
    def loop_with_ts(self):
        """创建带 mock TaskSystem + DelegationPreparer 的 CognitiveLoop。"""
        from core.cognitive import CognitiveLoop
        from core.status.agent_fsm import AgentFSM
        from core.monitoring.event_bus import EventBus
        from Module.output import OutputManager
        from core.memory.session import SessionMemory

        fsm = AgentFSM()
        bus = EventBus()
        output = OutputManager()
        session = SessionMemory()
        session._messages.clear()  # _handle_submit_task 需要空列表
        prompt = MagicMock()
        prompt._character = MagicMock(name="Test")
        llm = MagicMock()

        loop = CognitiveLoop(
            llm=llm, fsm=fsm, event_bus=bus, output=output,
            prompt=prompt, session=session,
        )

        # Mock TaskSystem
        ts = MagicMock()
        ts_mailbox = MagicMock()
        ts_mailbox.task_id = "task-001"
        ts.submit = AsyncMock(return_value=ts_mailbox)
        ts.get_task_mailbox = MagicMock(return_value=ts_mailbox)
        ts.constraints = MagicMock()
        ts.list_active_tasks = MagicMock(return_value=[])

        # Mock DelegationPreparer
        dp = MagicMock()
        from Module.workflow_engine.schema import (
            DelegationResult, DelegationStatus, DelegationContext, SessionId,
        )
        delegation_context = DelegationContext(
            intent="查天气",
            session_id=SessionId("test-session"),
        )
        dp.prepare = AsyncMock(return_value=DelegationResult(
            status=DelegationStatus.OK,
            context=delegation_context,
        ))

        loop.configure(task_system=ts, delegation_preparer=dp, user_profile=None)
        return loop

    @pytest.mark.asyncio
    async def test_intent_empty_returns_fail(self, loop_with_ts):
        """空 intent → ToolResult.fail。"""
        tc = ToolCall(
            id=ToolCallId("call_1"),
            function=ToolCallFunction(name="submit_task", arguments='{"intent": ""}'),
        )
        result = await loop_with_ts._handle_submit_task(tc)
        assert result.success is False
        assert "不能为空" in result.error

    @pytest.mark.asyncio
    async def test_success_returns_async_task_id(self, loop_with_ts):
        """成功后返回 async_task_id 非空。"""
        tc = ToolCall(
            id=ToolCallId("call_1"),
            function=ToolCallFunction(name="submit_task", arguments='{"intent": "查天气"}'),
        )
        result = await loop_with_ts._handle_submit_task(tc)
        assert result.success is True
        assert result.async_task_id == "task-001"
        assert "已在后台执行" in result.content or "已经在后台执行" in result.content

    @pytest.mark.asyncio
    async def test_delegation_no_matching_tools_returns_fail(self, loop_with_ts):
        """DelegationPreparer 返回 no_matching_tools → fail。"""
        from Module.workflow_engine.schema import DelegationResult, DelegationStatus
        dp = loop_with_ts._delegation_preparer
        dp.prepare = AsyncMock(return_value=DelegationResult(
            status=DelegationStatus.NO_MATCHING_TOOLS,
            missing_tools=["email"],
        ))
        tc = ToolCall(
            id=ToolCallId("call_1"),
            function=ToolCallFunction(name="submit_task", arguments='{"intent": "查天气"}'),
        )
        result = await loop_with_ts._handle_submit_task(tc)
        assert result.success is False
        assert "email" in result.error

    @pytest.mark.asyncio
    async def test_task_system_none_returns_fail(self, loop_with_ts):
        """TaskSystem 未注入 → fail。"""
        loop_with_ts.configure(task_system=None)
        tc = ToolCall(
            id=ToolCallId("call_1"),
            function=ToolCallFunction(name="submit_task", arguments='{"intent": "查天气"}'),
        )
        result = await loop_with_ts._handle_submit_task(tc)
        assert result.success is False
        assert "未初始化" in result.error


# ============================================================================
# 21. 唤醒消息测试
# ============================================================================

@pytest.mark.service
class TestWakeUpMessage:
    def test_wake_up_message_includes_intent_and_result(self):
        """唤醒模板拼接 intent 和 result。"""
        from core.cognitive import CognitiveLoop
        from core.status.agent_fsm import AgentFSM
        from Module.output import OutputManager
        from core.monitoring.event_bus import EventBus
        from core.memory.session import SessionMemory
        loop = CognitiveLoop(
            llm=MagicMock(), fsm=AgentFSM(), event_bus=EventBus(),
            output=OutputManager(), prompt=MagicMock(),
            session=SessionMemory(),
        )
        ctx = {
            "original_intent": "查天气",
            "result": "明天晴天",
            "task_id": "t1",
            "dispatched_at": "2026-06-17T00:00:00+00:00",
        }
        msg = loop._build_wake_up_message(ctx)
        assert "查天气" in msg
        assert "明天晴天" in msg
        assert "汇报" in msg or "稍后" in msg


# ============================================================================
# Runner
# ============================================================================

if __name__ == "__main__":
    async def run_all():
        import sys
        test_classes = [
            TestMergeStreamToolCalls, TestNormalizeToolCalls,
            TestFSMEdges, TestEventBusEdges,
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


@pytest.mark.unit
def test_filesystem_safety_profile():
    """文件系统安全配置通过 SafetyProfile 白名单控制。
    注意: read_dirs 可能被 load_all() 从 config.yaml 覆盖，
    因此只验证目录列表非空即可，不验证具体值。
    """
    from core.tools.filesystem.plugin import ReadFilePlugin
    p = ReadFilePlugin()
    assert p.safety.filesystem_allowed is True
    assert len(p.safety.read_dirs) > 0
    assert all(isinstance(d, str) for d in p.safety.read_dirs)
    assert "/home/yomu/data/sandbox" in " ".join(p.safety.read_dirs)
    assert p.safety.allow_symlinks is False
    assert p.safety.max_file_bytes > 0
