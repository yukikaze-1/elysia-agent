"""CharacterEngine 单元测试。"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from core.personality import CharacterEngine
from core.llm_adapter import ThoughtResult
from core.schema.character import CharacterState
from core.schema.types import SessionId


def _make_engine(state=None, llm=None, output=None):
    """创建测试用 CharacterEngine。"""
    s = state or CharacterState(session_id=SessionId("test-sid"))
    m = llm or MagicMock()
    m.think = AsyncMock(return_value=ThoughtResult(text=""))
    o = output or MagicMock()
    o.expression = MagicMock(return_value=MagicMock())
    from core.character_config import CharacterConfig
    char = CharacterConfig(name="Elysia")
    return CharacterEngine(
        llm=m,
        state=s,
        character_name="Elysia",
        user_name="妖梦",
        base_personality="你是 Elysia。温柔底色。",
        character_config=char,
        output=o,
    )


@pytest.mark.unit
class TestCharacterEngineInit:
    def test_loads_l1_core(self):
        """初始化时存储 L1 Core。"""
        engine = _make_engine()
        assert engine.render_c1().core_personality == "你是 Elysia。温柔底色。"

    def test_l2_self_is_empty_initially(self):
        """初始 L2 Self 为空。"""
        engine = _make_engine()
        assert engine.render_c1().self_personality == ""


@pytest.mark.unit
class TestCharacterEngineC9:
    def test_c9_empty_when_no_state(self):
        """无 L3 State 时 C9 返回空。"""
        engine = _make_engine()
        assert engine.render_c9().awareness_text == ""

    def test_c9_returns_state_when_triggered(self):
        """L3 State 存在且触发条件满足时 C9 返回内容。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        s.state_l3 = "最近比较敷衍"
        s.state_history = [{"timestamp": 0, "content": ""}]
        engine = _make_engine(state=s)
        # 手动触发一个事件以确保 awareness 激活
        asyncio.run(engine.consume_trigger("test", {"content": "测试触发"}))
        result = engine.render_c9()
        assert result.awareness_text


@pytest.mark.unit
class TestCharacterEngineTrigger:
    def test_consume_trigger_updates_state_l3(self):
        """consume_trigger 更新 state 中的 state_l3 和 history。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        asyncio.run(engine.consume_trigger(
            "reflector",
            {"content": "最近变得敷衍了"},
        ))
        assert s.state_l3
        assert "敷衍" in s.state_l3
        assert len(s.state_history) == 1

    def test_consume_trigger_no_llm_for_simple_state(self):
        """简单状态更新不调用 LLM。"""
        engine = _make_engine()
        asyncio.run(engine.consume_trigger("test", {"content": "test state"}))
        engine._llm.think.assert_not_called()


@pytest.mark.unit
class TestCharacterEngineCompression:
    """L3→L2 压缩测试。"""

    @staticmethod
    def _fill_state_history(state, count: int, base_text: str = "状态变化"):
        """填充 state_history 到指定条数。"""
        for i in range(count):
            state.state_history.append({"timestamp": time.time(), "content": f"{base_text} #{i+1}"})

    def test_should_compress_false_when_too_few_entries(self):
        """state_history 不足 3 条时不触发压缩。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        self._fill_state_history(s, 2)
        assert not engine._should_compress()

    def test_should_compress_true_when_enough_new_entries(self):
        """state_history 新增 >= 5 条时触发压缩。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        self._fill_state_history(s, 5)
        assert engine._should_compress()

    def test_should_compress_false_when_not_enough_new(self):
        """上次压缩后新增不足 5 条时不触发。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        self._fill_state_history(s, 7)
        s.compression_last_count = 3  # 仅 4 条新
        assert not engine._should_compress()

    def test_compress_updates_self_l2(self):
        """成功压缩后 self_l2 被写入。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        self._fill_state_history(s, 5)
        s.state_l3 = "最近变得温柔了"
        engine._llm.think.return_value = ThoughtResult(text="我似乎更在意妖梦的感受了")

        # Mock PromptManager
        mock_pm = MagicMock()
        mock_pm.render_macro.return_value = "rendered prompt"
        engine._pm = mock_pm

        asyncio.run(engine._compress())

        assert s.self_l2 == "我似乎更在意妖梦的感受了"
        assert s.compression_last_count == 5
        assert s.compression_last_at > 0

    def test_compress_no_overwrite_on_empty_result(self):
        """LLM 返回空时更新追踪但不覆盖已有 L2。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        s.self_l2 = "已有自我认知"
        self._fill_state_history(s, 5)
        engine._llm.think.return_value = ThoughtResult(text="")
        mock_pm = MagicMock()
        mock_pm.render_macro.return_value = "rendered prompt"
        engine._pm = mock_pm

        asyncio.run(engine._compress())

        assert s.self_l2 == "已有自我认知"  # 不变
        assert s.compression_last_count == 5  # 追踪更新

    def test_compress_llm_failure_preserves_state(self):
        """LLM 调用失败时追踪状态不变，下次重试。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        self._fill_state_history(s, 5)
        engine._llm.think.side_effect = RuntimeError("API 不可用")
        mock_pm = MagicMock()
        mock_pm.render_macro.return_value = "rendered prompt"
        engine._pm = mock_pm

        asyncio.run(engine._compress())

        assert s.compression_last_count == 0  # 不变
        assert s.compression_last_at is None  # 不变

    def test_lock_prevents_concurrent_compression(self):
        """Lock 被占用时跳过本次压缩。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        self._fill_state_history(s, 5)
        engine._llm.think.return_value = ThoughtResult(text="test")
        mock_pm = MagicMock()
        mock_pm.render_macro.return_value = "rendered prompt"
        engine._pm = mock_pm

        # 手动抢占 lock
        held = asyncio.run(self._acquire_lock(engine._compression_lock))
        assert held

        # 此时 _maybe_compress 应跳过
        asyncio.run(engine._maybe_compress())
        assert s.compression_last_count == 0  # 未被压缩

        # 释放后再次 trigger 应成功
        engine._compression_lock.release()
        asyncio.run(engine._maybe_compress())
        assert s.compression_last_count == 5

    @staticmethod
    async def _acquire_lock(lock) -> bool:
        await lock.acquire()
        return True
