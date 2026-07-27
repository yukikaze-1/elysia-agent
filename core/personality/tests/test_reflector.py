"""CharacterReflector 单元测试."""
import asyncio
import time
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.personality import CharacterReflector
from core.schema.character import CharacterState
from core.schema.types import SessionId


@pytest.mark.service
class TestCharacterReflector:
    """CharacterReflector 缓冲区、触发、flush 测试."""

    @pytest.fixture
    def llm(self):
        llm = AsyncMock()
        llm.think = AsyncMock(return_value=MagicMock(text="最近没什么特别的。"))
        return llm

    @pytest.fixture
    def state(self):
        return CharacterState(session_id=SessionId("test-sid"))

    @pytest.fixture
    def engine_mock(self, state):
        from unittest.mock import MagicMock
        engine = MagicMock()
        engine.state = state
        engine.consume_trigger = AsyncMock()
        return engine

    @pytest.fixture
    def reflector(self, llm, engine_mock):
        return CharacterReflector(
            engine=engine_mock,
            l1_core="你是 Elysia。温柔底色。",
            llm=llm,
            character_name="Elysia",
            user_name="妖梦",
            prompt_dir=str(Path(__file__).resolve().parent.parent.parent / "prompts"),
        )

    # ---- buffer 阈值 ----

    @pytest.mark.asyncio
    async def test_buffer_below_threshold_no_trigger(self, reflector, engine_mock):
        """缓冲区未达 50 对时不触发反思."""
        # 预置一条最近的历史，避免触发首次 10 对的特殊阈值
        engine_mock.state.self_perception_history = [
            {"timestamp": time.time(), "content": "最近没什么特别的。"}
        ]
        for _ in range(49):
            await reflector.buffer("你好", "你好呀")
        # 没有触发新反思 — 历史仍只有预置的那一条
        assert len(engine_mock.state.self_perception_history) == 1

    @pytest.mark.asyncio
    async def test_buffer_at_threshold_triggers_reflect(self, reflector, engine_mock):
        """缓冲区达到 50 对时触发反思."""
        # 预置一条最近的历史，禁用首次 10 对的特殊阈值
        engine_mock.state.self_perception_history = [
            {"timestamp": time.time(), "content": "最近没什么特别的。"}
        ]
        # 49 对不应触发 — 不到 50 对且不到 24h
        for i in range(49):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await asyncio.sleep(0.1)
        assert len(engine_mock.state.self_perception_history) == 1  # 还是预置的那一条
        # 第 50 对应触发反思
        await reflector.buffer("msg50", "reply50")
        await asyncio.sleep(0.1)
        # 验证 consume_trigger 被调用
        engine_mock.consume_trigger.assert_called_once()

    # ---- 24h 时间触发 ----

    @pytest.mark.asyncio
    async def test_24h_trigger_even_with_fewer_rounds(self, reflector, engine_mock):
        """距上次反思超过 24h 时，即使不足 50 对也触发."""
        old_ts = time.time() - 25 * 3600  # 25 小时前
        engine_mock.state.self_perception_history = [
            {"timestamp": old_ts, "content": "旧认知"}
        ]
        # 只加 10 对，但因为超 24h 应该触发
        for i in range(10):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await asyncio.sleep(0.1)
        engine_mock.consume_trigger.assert_called_once()

    # ---- lock 防并发 ----

    @pytest.mark.asyncio
    async def test_lock_prevents_concurrent_reflect(self, reflector):
        """_reflect_lock 防止并发 LLM 调用."""
        # 手动获取锁模拟正在反思
        await reflector._reflect_lock.acquire()
        try:
            for i in range(50):
                await reflector.buffer(f"msg{i}", f"reply{i}")
            # buffer 应该被消费但 LLM 没有被调用（锁被持有，跳过）
            assert len(reflector._buffer) >= 50  # 锁住时不清 buffer
        finally:
            reflector._reflect_lock.release()

    # ---- flush ----

    @pytest.mark.asyncio
    async def test_flush_with_buffered_turns(self, reflector, engine_mock):
        """flush() 在 buffer >= 10 对时强制反思并清空."""
        # 预设 history 使阈值回到 50（避免首次 10 对自动触发）
        engine_mock.state.self_perception_history = [
            {"timestamp": time.time(), "content": "old"}
        ]
        for i in range(12):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await reflector.flush()
        assert len(reflector._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_preserves_small_buffer(self, reflector, engine_mock):
        """flush() 在 buffer < 10 对时保留缓冲."""
        for i in range(5):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await reflector.flush()
        assert len(reflector._buffer) == 5  # 保留，不被清空

    @pytest.mark.asyncio
    async def test_flush_empty_buffer_noop(self, reflector, engine_mock):
        """flush() 在空缓冲区时不做任何事."""
        await reflector.flush()
        reflector._llm.think.assert_not_called()

    # ---- debug 属性 ----

    def test_buffer_size_property(self, reflector):
        """buffer_size 返回当前缓冲轮数。"""
        assert reflector.buffer_size == 0
        reflector._buffer.append(("hi", "hello"))
        assert reflector.buffer_size == 1

    def test_locked_property(self, reflector):
        """locked 在无锁时为 False。"""
        assert reflector.locked is False

    @pytest.mark.asyncio
    async def test_last_reflection_at_updated_after_reflect(self, reflector, engine_mock):
        """last_reflection_at 在反思后更新。"""
        assert reflector.last_reflection_at is None
        reflector._llm.think.return_value = MagicMock(text="新的感受")
        for i in range(50):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await asyncio.sleep(0.1)
        assert reflector.last_reflection_at is not None
        assert reflector.last_success is True
        assert reflector.last_text_len > 0

    @pytest.mark.asyncio
    async def test_last_success_false_on_llm_failure(self, reflector, engine_mock):
        """LLM 调用失败时 last_success 为 False。"""
        reflector._llm.think.side_effect = Exception("模拟失败")
        engine_mock.state.self_perception_history = [
            {"timestamp": time.time() - 25*3600, "content": "旧"}
        ]
        for i in range(10):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await asyncio.sleep(0.1)
        assert reflector.last_success is False
        assert reflector.last_duration_ms > 0


@pytest.mark.service
def test_reflector_submits_to_character_engine():
    """Reflector 完成后提交事件到 CharacterEngine。"""
    from core.personality import CharacterEngine
    from core.schema.character import CharacterState
    from core.schema.types import SessionId
    from unittest.mock import AsyncMock, MagicMock

    s = CharacterState(session_id=SessionId("test-sid"))
    llm = MagicMock()
    llm.think = AsyncMock(return_value=MagicMock(text="我注意到自己变得敷衍了"))

    # Mock output and character config
    output = MagicMock()
    output.expression = MagicMock(return_value=MagicMock())
    from core.config import CharacterConfig
    char = CharacterConfig(name="Elysia")

    engine = CharacterEngine(
        llm=llm, state=s,
        character_name="Elysia", user_name="妖梦",
        base_personality="温柔底色",
        character_config=char,
        output=output,
    )

    # 快速填充 buffer 达到触发阈值
    reflector_ref = engine._reflector
    for _ in range(10):
        reflector_ref._buffer.append(("你好", "你好呀"))

    asyncio.run(reflector_ref.flush())
    assert s.state_l3
    assert "敷衍" in s.state_l3
