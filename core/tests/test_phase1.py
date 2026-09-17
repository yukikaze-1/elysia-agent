"""阶段 1 综合测试：流式输出、中断、情绪、句子缓冲。

Run:
    python -m pytest core/test_phase1.py -v
or:
    python core/test_phase1.py
"""

import asyncio
import os

import pytest

from core.schema import RequestId
from core.schema.event import InterruptData, StateChangeData, UserInputData


@pytest.fixture
def real_env(monkeypatch):
    """把项目根 `.env` **按需**载入本用例的 os.environ，用例结束自动还原。

    代替测试体内的裸 `load_dotenv()`（Batch 1.5 实测：它把 .env 的 13 个键
    **永久**写进进程 env——`pytest core/tests/test_phase1.py -m integration` 一条
    用例即可让后续同进程测试全部看到真实凭据）。语义与 dotenv 默认一致
    （`override=False`：只补缺失的键），但作用域收在本用例内。

    这些 integration/slow 用例**确实需要**真实凭据才跑得起来（真实 LLM 调用），
    故保留"载入 .env"这一行为本身，只把进程级写入改为用例级。
    """
    from dotenv import dotenv_values

    from paths import get_project_root

    for key, value in dotenv_values(get_project_root() / ".env").items():
        if value is not None and key not in os.environ:
            monkeypatch.setenv(key, value)

# ============================================================================
# 0. 测试基建：门控 fake LLM + 有界轮询
# ============================================================================
#
# 中断测试的两条纪律（2026-09-10 复审）：
#   1. 「生成在途」必须是**可控**状态，不能靠 sleep 猜时序；
#   2. 判据必须是「本轮生成**没有跑完**」，不能只看 FSM==IDLE——真实 LLM
#      调用失败时 agent 同样回 IDLE，故只断言 IDLE 的版本在无 key/断网时也绿。
# 存储经 `_make_agent_with` 注入内存 fake，测试不写真实 Mongo 的 default 会话。


async def _wait_until(pred, *, timeout: float = 15.0, interval: float = 0.02) -> None:
    """有界轮询直到 pred() 成立；超时抛 AssertionError。

    替代固定 sleep：中断若回归成不生效，测试在此以断言失败结束，而不是挂死
    或靠运气等够时间。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"等待超时（{timeout}s）：条件始终未成立")


class _GatedLLM:
    """门控 fake LLM：生成进入即计数并**阻塞**，直到被放行或被取消。

    `started` / `completed` 是中断测试的核心判据——断言「本轮进入了 LLM 但
    从未跑完」，即生成确实被打断。镜像 `_UsageLLM` 接口（`_model`/`chat`/
    `test`/`close`/`stream_think`），loop 经 LLMAdapter 调用。
    """

    def __init__(self, response: str = "这是一段足够长的回复，用于在途中断测试"):
        self.response = response
        self.started = 0
        self.completed = 0
        self.release = asyncio.Event()  # 默认不放行：生成停在在途状态
        self._model = "fake/test"

    async def chat(self, messages, **kwargs):
        # 返回合法空 JSON 数组：chat() 也被后台记忆整合（MemoryConsolidator 经
        # LLMAdapter.think）调用，它要求 JSON 数组。返回叙述文本会让整合在后台抛
        # _JsonArrayParseError，污染本测试的判定。
        return "[]"

    async def test(self) -> dict:
        return {"ok": True, "latency_ms": 0}

    async def close(self) -> None:
        pass

    async def stream_think(self, messages, **kwargs):
        self.started += 1
        await self.release.wait()  # 在途；被取消则 CancelledError 自此抛出
        for tok in self.response:
            yield {"content": tok, "tool_calls": None}
        yield {"content": None, "tool_calls": None}
        self.completed += 1


class _RecordingLLM:
    """记录每次 stream_think 收到的 messages——供断言「上下文确实传到了 LLM」。"""

    def __init__(self, response: str = "我记得。"):
        self.response = response
        self.calls: list[list[dict]] = []
        self._model = "fake/test"

    async def chat(self, messages, **kwargs):
        # 返回合法空 JSON 数组——chat() 同时被后台记忆整合（LLMAdapter.think →
        # MemoryConsolidator）调用并要求 JSON 数组；返回叙述文本会让整合在后台抛
        # _JsonArrayParseError，把本测试的失败原因搅成无关异常。
        return "[]"

    async def test(self) -> dict:
        return {"ok": True, "latency_ms": 0}

    async def close(self) -> None:
        pass

    async def stream_think(self, messages, **kwargs):
        self.calls.append(list(messages))
        for tok in self.response:
            yield {"content": tok, "tool_calls": None}
        yield {"content": None, "tool_calls": None}


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

    @pytest.mark.external
    @pytest.mark.asyncio
    async def test_real_stream_think_structured(self, real_env):
        """真实 LLM 结构化流式输出 content 字典。"""
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

    @pytest.mark.external
    @pytest.mark.asyncio
    async def test_streaming_delivers_tokens_immediately(self, real_env):
        """第一个块应快速到达，而不是在整个响应完成后。"""
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
# 6. Interrupt wiring tests（service：门控 fake LLM + 内存 fake 存储，离线秒级）
# ============================================================================
#
# 分层说明（2026-09-10）：本类原标 `slow` 并打真实 DeepSeek API。改用 `_GatedLLM`
# 门控 fake + `_make_agent_with` 注入的内存 fake 存储后，用例不再连真实 LLM/DB，
# 故归 `service` 进 PR 门禁。同 `core/cognitive/tests/test_v2_generation.py`
# （同一 `_make_agent_with` 基建，`pytestmark = service`，同样在进程内
# `agent.start()`）的先例——「不启动 Agent」约束的是**连真实依赖**的启动。

@pytest.mark.service
class TestInterruptWiring:
    @pytest.mark.asyncio
    async def test_interrupt_during_processing(self):
        """中断在途生成：LLM 流被真正打断（未跑完），agent 回 idle，generation 落 cancelled。

        经 `do_interrupt()`——即 WS `{"type":"interrupt"}` 与 VAD 按钮的**真实入口**
        （内部 post `InterruptData(source="websocket")` 到总线）。`test_generation_wiring.py`
        有一套 0.05s 的同类用例，但它是**直接调** `loop.cancel_current()`、绕过了总线；
        本用例补的正是「总线 → 认知循环」这段接线。
        """
        from core.cognitive.tests.test_v2_generation import _make_agent_with
        from core.input.ws_handler import do_interrupt

        llm = _GatedLLM()
        agent = _make_agent_with(llm)
        await agent.start()
        try:
            await agent.process_message("写一首长诗，越多越好")
            # 精确等到生成已进入 LLM（而非 sleep 猜时序）——中断此时才打在在途生成上
            await _wait_until(lambda: llm.started >= 1)
            await _wait_until(lambda: not agent.status.is_completely_idle, timeout=5)

            await do_interrupt(agent)
            await _wait_until(lambda: agent.status.is_completely_idle)

            assert llm.completed == 0, "生成应在途被打断，不得跑完"
            reg = agent.cognitive._generation_registry  # noqa: SLF001 — 断言终态需读注册表
            docs, _, _ = await reg.query_by_session("default")
            assert docs, "应留有 generation 记录"
            assert docs[-1]["status"] == "cancelled", \
                f"总线中断应落 cancelled，实际 {docs[-1]['status']}"
        finally:
            llm.release.set()  # 防御：若断言前未被取消，放行以免 stop() 挂住
            await agent.stop()

    @pytest.mark.asyncio
    async def test_new_message_after_interrupt(self):
        """中断后 agent 仍可继续工作：新消息能被受理并**完整跑完**。

        实质是「中断没有把 agent 弄坏」——故断言第二轮生成真的完成（而非只看
        有消息落库：旧版断言 `len(messages) > 0` 恒真，任何一次用户消息入 session
        都满足，伪造 key 下也照样绿）。
        """
        from core.cognitive.tests.test_v2_generation import _make_agent_with
        from core.input.ws_handler import do_interrupt

        llm = _GatedLLM()
        agent = _make_agent_with(llm)
        await agent.start()
        try:
            # 第一轮：在途打断
            await agent.process_message("写一首很长的诗，越长越好")
            await _wait_until(lambda: llm.started >= 1)
            await do_interrupt(agent)
            await _wait_until(lambda: agent.status.is_completely_idle)
            assert llm.completed == 0, "第一轮应被中断，不得跑完"

            # 第二轮：放行门控，应能正常受理并跑完
            llm.release.set()
            await agent.process_message("你好呀")
            await _wait_until(lambda: llm.started >= 2, timeout=20)
            await _wait_until(lambda: llm.completed >= 1, timeout=20)

            assert llm.completed == 1, "第二轮的生成应完整跑完一次"
            # 助手消息落 session 在 LLM 流结束**之后**（两者之间有窗口），故须
            # 轮询等它出现，不能紧跟 completed 就断言。
            await _wait_until(
                lambda: any(m.role == "assistant"
                            for m in agent.session.recent_messages()),
                timeout=20,
            )
        finally:
            llm.release.set()
            await agent.stop()

    @pytest.mark.asyncio
    async def test_multiple_interrupts(self):
        """连续三轮中断：每轮生成都在途被打断，agent 每次都回到 idle。

        「多次不崩」的实质是「每一轮都确实被中断」——故逐轮断言 LLM 未跑完，
        而非只看最后是否 idle（后者在「第三轮才生效」时也会绿）。
        """
        from core.cognitive.tests.test_v2_generation import _make_agent_with
        from core.input.ws_handler import do_interrupt

        llm = _GatedLLM()
        agent = _make_agent_with(llm)
        await agent.start()
        try:
            for round_no in (1, 2, 3):
                await agent.process_message(f"第 {round_no} 轮：写一首诗")
                await _wait_until(lambda n=round_no: llm.started >= n)
                await do_interrupt(agent)
                await _wait_until(lambda: agent.status.is_completely_idle)
                assert llm.completed == 0, f"第 {round_no} 轮生成未被中断（跑完了）"
            assert llm.started == 3, f"三轮应各进入一次 LLM，实际 {llm.started}"
        finally:
            llm.release.set()
            await agent.stop()


# ============================================================================
# 7. Conversation context（service）与角色 prompt 注入（slow）测试
# ============================================================================

@pytest.mark.service
class TestConversationContext:
    @pytest.mark.asyncio
    async def test_session_context_preserved(self):
        """前几轮对话应作为**上下文传给 LLM**，而不只是留在本地 session 里。

        断言第二轮请求的 messages 同时含第一轮的用户消息与助手回复——这正是
        「上下文保留」的实质。旧版只断言 `session.recent_messages()` 含第一轮的
        用户消息，而该消息在调 LLM **之前**就已入 session，故上下文组装即使完全
        坏掉（第二轮不带历史）也照样绿。
        """
        import json as _json

        from core.cognitive.tests.test_v2_generation import _make_agent_with

        llm = _RecordingLLM()
        agent = _make_agent_with(llm)
        await agent.start()
        try:
            await agent.process_message("我叫妖梦")
            await _wait_until(lambda: agent.status.is_completely_idle,
                              timeout=20)

            await agent.process_message("你还记得我叫什么吗")
            # 等到「含第二轮问题的那个请求」真正发出——它就是待检的上下文载体
            def _second_turn_request() -> bool:
                return any(
                    "你还记得我叫什么吗" in _json.dumps(c, ensure_ascii=False)
                    for c in llm.calls
                )

            await _wait_until(_second_turn_request, timeout=20)
            second = next(
                c for c in llm.calls
                if "你还记得我叫什么吗" in _json.dumps(c, ensure_ascii=False)
            )

            joined = _json.dumps(second, ensure_ascii=False)
            assert "我叫妖梦" in joined, "第二轮请求应带上第一轮的用户消息"
            assert any(m.get("role") == "assistant" for m in second), \
                "第二轮请求应带上第一轮的助手回复"
        finally:
            await agent.stop()


@pytest.mark.slow
class TestCharacterPromptInjection:
    # 保留 slow：本用例载入 .env（real_env fixture，用例结束还原）+ 真实
    # LongTermMemory（构造即读 journal）+ 读模板文件，属真 I/O；
    # 仅由手工触发的 `make test-full` 覆盖。
    @pytest.mark.asyncio
    async def test_character_personality_injected(self, real_env):
        """验证角色配置（base_personality）出现在 LLM 请求中。"""
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
#
# 原 `TestEdgeCases::test_empty_message` 已删（2026-09-10 复审）：它起完整 Agent
# 后断言 `state in ("idle", "thinking")` —— AgentState 共 6 个值、该断言接受 2 个，
# 且原文注释自述 "Could be idle or still in prev state"，实为恒真。空消息处理在
# 快层已有实质覆盖（`core/input/tests/test_server.py` 的
# `TestHandleJsonEdges::test_empty_text_not_processed` 等，marker=unit）。




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
