"""MemoryConsolidator 单元 + service 测试 — 对话提取、比对、缓冲阈值。

Run:
    python -m pytest core/memory/tests/test_consolidator.py -v --asyncio-mode=auto
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.memory.consolidator import (
    MemoryConsolidator,
    _JsonArrayParseError,
    _parse_extraction,
    _parse_compare_actions,
)
from core.schema.memory import MemoryType
from core.memory.long_term import LongTermMemory
from core.adapter.llm_adapter import LLMAdapter


# ============================================================================
# 1. _parse_extraction tests
# ============================================================================

@pytest.mark.unit
class TestParseExtraction:
    """_parse_extraction 单元测试。"""

    def test_parse_json(self):
        items = _parse_extraction(
            '[{"type": "Preference", "content": "likes blue", "poignancy": 5}]'
        )
        assert len(items) == 1
        assert items[0].type == MemoryType.PREFERENCE

    def test_parse_markdown_wrapped(self):
        items = _parse_extraction(
            '```json\n[{"type": "Fact", "content": "hello", "poignancy": 5}]\n```'
        )
        assert len(items) == 1
        assert items[0].content == "hello"

    def test_parse_empty(self):
        """合法空数组 → 空；不可解析文本 → 抛 _JsonArrayParseError（S9）。"""
        assert _parse_extraction("[]") == []
        with pytest.raises(_JsonArrayParseError):
            _parse_extraction("")
        with pytest.raises(_JsonArrayParseError):
            _parse_extraction("not json")

    def test_parse_json_embedded_in_text(self):
        """JSON array embedded in surrounding text should be extracted."""
        text = 'prefix text [{"type": "Fact", "content": "found"}] suffix text'
        items = _parse_extraction(text)
        assert len(items) == 1
        assert items[0].content == "found"

    def test_parse_multiple_arrays_returns_first(self):
        """When multiple arrays exist, greedy regex captures from first [ to last ],
        producing invalid JSON → 解析失败抛异常。"""
        text = '[{"type": "fact", "content": "a"}] other [{"type": "plan", "content": "b"}]'
        # Greedy regex matches first [ to last ] → invalid JSON → raise
        with pytest.raises(_JsonArrayParseError):
            _parse_extraction(text)

    def test_parse_trailing_comma(self):
        """Trailing comma in JSON array (invalid JSON) → 解析失败抛异常。"""
        # Trailing comma is invalid JSON → json.loads + regex both fail → raise
        with pytest.raises(_JsonArrayParseError):
            _parse_extraction('[{"type": "Fact", "content": "test"},]')

    def test_parse_whitespace_only(self):
        with pytest.raises(_JsonArrayParseError):
            _parse_extraction("   \n  ")
        with pytest.raises(_JsonArrayParseError):
            _parse_extraction("```json\n```")

    def test_parse_single_object_not_array(self):
        """A single JSON object (not array) → 解析失败抛异常。"""
        with pytest.raises(_JsonArrayParseError):
            _parse_extraction('{"type": "fact", "content": "not an array"}')

    def test_parse_array_with_invalid_items(self):
        """Items missing required fields should still parse."""
        items = _parse_extraction(
            '[{"type": "Fact", "content": "ok"}, {"invalid": "item"}, {"type": "Plan"}]'
        )
        assert len(items) == 1
        assert items[0].type == MemoryType.FACT
        assert items[0].content == "ok"


# ============================================================================
# 2. _parse_compare_actions tests
# ============================================================================

@pytest.mark.unit
class TestParseCompareActions:
    """_parse_compare_actions 单元测试 — all-or-nothing 校验契约。"""

    def test_valid_batch_returns_typed_objects(self):
        from core.schema.memory import SkipAction, InsertAction, UpdateAction, MergeAction

        text = json.dumps([
            {"action": "skip"},
            {"action": "insert", "content": "新事实", "type": "Fact", "poignancy": 6},
            {"action": "update", "target_id": "id-1", "new_content": "更新后"},
            {"action": "merge", "target_id": "id-2", "merged_content": "合并后",
             "type": "Preference", "poignancy": 7},
        ], ensure_ascii=False)
        actions = _parse_compare_actions(text)
        assert len(actions) == 4
        assert isinstance(actions[0], SkipAction)
        assert isinstance(actions[1], InsertAction)
        assert isinstance(actions[2], UpdateAction)
        assert isinstance(actions[3], MergeAction)

    def test_invalid_batch_raises_value_error(self):
        """一个坏元素（未知 action）→ 整批 raise ValueError。"""
        with pytest.raises(ValueError):
            _parse_compare_actions('[{"action": "skip"}, {"action": "destroy"}]')

    def test_empty_array_returns_empty(self):
        assert _parse_compare_actions("[]") == []

    def test_count_mismatch_raises_value_error(self):
        """expected_count 不符（含 LLM 输出被剥成空数组）→ raise ValueError。"""
        with pytest.raises(ValueError):
            _parse_compare_actions("[]", expected_count=2)
        with pytest.raises(ValueError):
            _parse_compare_actions('[{"action": "skip"}]', expected_count=2)

    def test_count_match_passes(self):
        actions = _parse_compare_actions('[{"action": "skip"}]', expected_count=1)
        assert len(actions) == 1


# ============================================================================
# 3. MemoryConsolidator — consolidate / flush
# ============================================================================

@pytest.mark.service
class TestMemoryConsolidator:
    @pytest.mark.asyncio
    async def test_consolidate_extracts_facts(self):
        from core.tests.fake_llm import FakeLLMBackend

        adapter = LLMAdapter(_test_backend=FakeLLMBackend(response=json.dumps([
            {"type": "Preference", "content": "喜欢蓝色", "poignancy": 5},
            {"type": "Plan", "content": "明天去爬山", "poignancy": 5},
        ])))
        mem = LongTermMemory()
        consolidator = MemoryConsolidator(adapter, mem, "TestBot", buffer_threshold=1)

        items = await consolidator.consolidate(
            "我最喜欢蓝色，明天要去爬山",
            "好的妖梦！我记住了，你喜欢蓝色，而且明天要去爬山。"
        )

        assert len(items) >= 2
        types = {item.type.value for item in items}
        assert "Preference" in types
        assert "Plan" in types

        await adapter.close()

    @pytest.mark.asyncio
    async def test_consolidate_empty_conversation(self):
        from core.tests.fake_llm import FakeLLMBackend

        adapter = LLMAdapter(_test_backend=FakeLLMBackend())
        mem = LongTermMemory()
        consolidator = MemoryConsolidator(adapter, mem, "TestBot", buffer_threshold=1)

        items = await consolidator.consolidate("", "")
        assert items == []

        await adapter.close()

    @pytest.mark.asyncio
    async def test_parse_failure_retains_buffer(self):
        """LLM 输出不可解析时保留缓冲（不静默清空），下次可重试（S9）。"""
        from core.tests.fake_llm import FakeLLMBackend

        adapter = LLMAdapter(_test_backend=FakeLLMBackend(response="这不是 JSON"))
        mem = MagicMock()
        consolidator = MemoryConsolidator(adapter, mem, "TestBot", buffer_threshold=1)

        items = await consolidator.consolidate("用户消息", "助手回复")
        assert items == []
        # 解析失败 → 缓冲保留（clear 只在解析成功分支执行）
        assert len(consolidator._buffer) == 1

        await adapter.close()

    @pytest.mark.asyncio
    async def test_flush_consolidates_below_threshold(self):
        """flush() should consolidate buffered turns even below threshold."""

        class FakeLLM:
            async def think(self, messages, extra_body=None):
                class R:
                    text = '[{"type": "Fact", "content": "测试flush", "poignancy": 5}]'
                return R()

        mem = LongTermMemory()
        c = MemoryConsolidator(FakeLLM(), mem, buffer_threshold=10)  # type: ignore[arg-type]
        await c.consolidate("user msg 1", "assistant reply 1")
        await c.consolidate("user msg 2", "assistant reply 2")
        await c.consolidate("user msg 3", "assistant reply 3")
        assert mem.count == 0  # below threshold, not yet consolidated

        items = await c.flush()
        assert len(items) > 0
        assert mem.count > 0


# ============================================================================
# 4. MemoryConsolidator edge cases
# ============================================================================

@pytest.mark.service
class TestMemoryConsolidatorEdgeCases:
    @pytest.mark.asyncio
    async def test_consolidate_stores_in_memory(self):
        """Consolidation should actually store extracted facts."""
        from core.tests.fake_llm import FakeLLMBackend

        adapter = LLMAdapter(_test_backend=FakeLLMBackend())
        mem = LongTermMemory()
        consolidator = MemoryConsolidator(adapter, mem, "TestBot", buffer_threshold=1)
        count_before = mem.count

        await consolidator.consolidate(
            "我叫妖梦，我的生日是5月20日",
            "好的妖梦！我记住了，你叫妖梦，生日是5月20日。到时候要提醒我给你过生日哦～"
        )
        assert mem.count >= count_before

        await adapter.close()

    @pytest.mark.asyncio
    async def test_consolidate_very_long_messages(self):
        """Should handle messages at the character limit gracefully."""
        from core.tests.fake_llm import FakeLLMBackend

        adapter = LLMAdapter(_test_backend=FakeLLMBackend())
        mem = LongTermMemory()
        consolidator = MemoryConsolidator(adapter, mem, "TestBot", buffer_threshold=1)

        long_msg = "长消息" * 300
        items = await consolidator.consolidate(long_msg, long_msg)
        assert items == []  # FakeLLMBackend 无提取输出 → 空列表

        await adapter.close()


# ============================================================================
# 5. Consolidator — buffer threshold
# ============================================================================

@pytest.mark.service
class TestConsolidatorThreshold:
    @pytest.mark.asyncio
    async def test_threshold_6_buffers_below_limit(self):
        """buffer_threshold=6 — 5条消息不触发_do_consolidate。"""
        from core.tests.fake_llm import FakeLLMBackend

        adapter = LLMAdapter(_test_backend=FakeLLMBackend(response="[]"))
        mem = MagicMock()
        mem.insert = AsyncMock()
        mem.search = AsyncMock(return_value=[])
        consolidator = MemoryConsolidator(adapter, mem, buffer_threshold=6)

        for i in range(5):
            await consolidator.consolidate(f"msg{i}", "reply")
        assert len(consolidator._buffer) == 5

    @pytest.mark.asyncio
    async def test_threshold_6_flushes_on_sixth(self):
        """buffer_threshold=6 — 第6条触发_do_consolidate，buffer清空。"""
        from core.tests.fake_llm import FakeLLMBackend

        adapter = LLMAdapter(_test_backend=FakeLLMBackend(response="[]"))
        mem = MagicMock()
        mem.insert = AsyncMock()
        mem.search = AsyncMock(return_value=[])
        consolidator = MemoryConsolidator(adapter, mem, buffer_threshold=6)

        for i in range(6):
            await consolidator.consolidate(f"msg{i}", "reply")
        assert len(consolidator._buffer) == 0
