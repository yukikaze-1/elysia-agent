"""CharacterReflector 单元测试."""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.character_reflector import CharacterReflector


class TestCharacterReflector:
    """CharacterReflector 缓冲区、触发、flush 测试."""

    @pytest.fixture
    def llm(self):
        llm = AsyncMock()
        llm.think = AsyncMock(return_value=MagicMock(text="最近没什么特别的。"))
        return llm

    @pytest.fixture
    def session(self):
        s = MagicMock()
        s._params = {}
        s.set_param = lambda k, v: s._params.__setitem__(k, v)
        s.get_param = lambda k, d=None: s._params.get(k, d)
        return s

    @pytest.fixture
    def reflector(self, llm, session):
        return CharacterReflector(
            llm=llm,
            session=session,
            character_name="Elysia",
            user_name="妖梦",
            prompt_dir="prompt",
        )

    # ---- buffer 阈值 ----

    @pytest.mark.asyncio
    async def test_buffer_below_threshold_no_trigger(self, reflector):
        """缓冲区未达 50 对时不触发反思."""
        # 预置一条最近的历史，避免触发首次 10 对的特殊阈值
        reflector._session._params["self_perception_history"] = [
            {"timestamp": time.time(), "content": "最近没什么特别的。"}
        ]
        for _ in range(49):
            await reflector.buffer("你好", "你好呀")
        # 没有触发新反思 — 历史仍只有预置的那一条
        assert len(reflector._session._params["self_perception_history"]) == 1

    @pytest.mark.asyncio
    async def test_buffer_at_threshold_triggers_reflect(self, reflector):
        """缓冲区达到 50 对时触发反思."""
        # 预置一条最近的历史，禁用首次 10 对的特殊阈值
        reflector._session._params["self_perception_history"] = [
            {"timestamp": time.time(), "content": "最近没什么特别的。"}
        ]
        # 49 对不应触发 — 不到 50 对且不到 24h
        for i in range(49):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await asyncio.sleep(0.1)
        assert len(reflector._session._params["self_perception_history"]) == 1  # 还是预置的那一条
        # 第 50 对应触发反思
        await reflector.buffer("msg50", "reply50")
        await asyncio.sleep(0.1)
        assert len(reflector._session._params["self_perception_history"]) >= 2  # 预置 + 新反思

    # ---- 24h 时间触发 ----

    @pytest.mark.asyncio
    async def test_24h_trigger_even_with_fewer_rounds(self, reflector, llm):
        """距上次反思超过 24h 时，即使不足 50 对也触发."""
        old_ts = time.time() - 25 * 3600  # 25 小时前
        reflector._session._params["self_perception_history"] = [
            {"timestamp": old_ts, "content": "旧认知"}
        ]
        # 只加 10 对，但因为超 24h 应该触发
        for i in range(10):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await asyncio.sleep(0.1)
        history = reflector._session.get_param("self_perception_history", [])
        assert len(history) >= 2  # 旧认知 + 新反思

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
    async def test_flush_with_buffered_turns(self, reflector, llm):
        """flush() 强制反思已缓冲的轮次."""
        for i in range(5):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await reflector.flush()
        assert len(reflector._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_empty_buffer_noop(self, reflector, llm):
        """flush() 在空缓冲区时不做任何事."""
        await reflector.flush()
        llm.think.assert_not_called()

    # ---- 空结果不追加 ----

    @pytest.mark.asyncio
    async def test_empty_reflection_not_appended(self, reflector, llm):
        """空反思结果不追加到历史."""
        llm.think.return_value = MagicMock(text="")
        for i in range(50):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await asyncio.sleep(0.1)
        history = reflector._session.get_param("self_perception_history", [])
        # 不应该有新的空条目
        assert all(h["content"].strip() for h in history)

    # ---- 历史上限 ----

    @pytest.mark.asyncio
    async def test_history_capped_at_50(self, reflector, llm):
        """self_perception_history 上限 50 条，超出截掉最旧的."""
        old_entries = [{"timestamp": float(i), "content": f"entry{i}"} for i in range(50)]
        reflector._session._params["self_perception_history"] = old_entries
        llm.think.return_value = MagicMock(text="新认知")
        for i in range(50):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await asyncio.sleep(0.1)
        history = reflector._session.get_param("self_perception_history", [])
        assert len(history) <= 50
        assert history[-1]["content"] == "新认知"
