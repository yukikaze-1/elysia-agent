"""MemoryConsolidator 单元 + service 测试 — 对话提取、比对、缓冲阈值。

Run:
    python -m pytest core/memory/tests/test_consolidator.py -v --asyncio-mode=auto
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.memory.consolidator import (
    MemoryConsolidator,
    _JsonArrayParseError,
    _get_compare_prompt,
    _get_extraction_prompt,
    _parse_extraction,
    _parse_compare_actions,
)
from core.schema.memory import MemoryType
from core.memory.long_term import LongTermMemory
from core.memory.scope import MemoryScope


@pytest.fixture(autouse=True)
def _isolate_pending_syncs(tmp_path, monkeypatch):
    """review 复核 #6：pending-syncs journal 隔离到 tmp（跨测试串扰防护，同 test_long_term）。"""
    monkeypatch.setattr(
        LongTermMemory, "_pending_syncs_file",
        lambda self: str(tmp_path / "memory_pending_syncs.json"),
    )
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
        """应能提取嵌在周围文本中的 JSON 数组。"""
        text = 'prefix text [{"type": "Fact", "content": "found"}] suffix text'
        items = _parse_extraction(text)
        assert len(items) == 1
        assert items[0].content == "found"

    def test_parse_multiple_arrays_returns_first(self):
        """存在多个数组时，贪婪正则从第一个 [ 捕获到最后一个 ]，
        产生非法 JSON → 解析失败抛异常。"""
        text = '[{"type": "fact", "content": "a"}] other [{"type": "plan", "content": "b"}]'
        # 贪婪正则从第一个 [ 匹配到最后一个 ] → 非法 JSON → 抛异常
        with pytest.raises(_JsonArrayParseError):
            _parse_extraction(text)

    def test_parse_trailing_comma(self):
        """Trailing comma in JSON array (invalid JSON) → 解析失败抛异常。"""
        # 尾逗号是非法 JSON → json.loads 与正则均失败 → 抛异常
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
        """缺少必填字段的条目仍应能解析。"""
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
        """即使未达阈值，flush() 也应对缓冲的对话轮次进行整合。"""

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

    @pytest.mark.asyncio
    async def test_concurrent_consolidate_keeps_new_messages(self):
        """B5/P1-11：第一轮 consolidation 等待 LLM 时，第二轮新消息不被 clear 掉。"""
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingLLM:
            async def think(self, messages, extra_body=None):
                entered.set()
                await release.wait()

                class R:
                    text = "[]"
                return R()

        mem = LongTermMemory()
        c = MemoryConsolidator(BlockingLLM(), mem, "Bot", buffer_threshold=2)  # type: ignore[arg-type]

        t1 = asyncio.create_task(c.consolidate("u1", "a1"))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(c.consolidate("u2", "a2"))  # 触发阈值并阻塞在 LLM
        await asyncio.wait_for(entered.wait(), 1.0)
        assert c._buffer == []  # batch(u1,u2) 已原子 detach  # noqa: SLF001

        # 第二轮消息 append 到 fresh buffer
        await c.consolidate("u3", "a3")
        assert len(c._buffer) == 1  # 未被第一轮 clear 掉  # noqa: SLF001

        release.set()
        await asyncio.gather(t1, t2)
        # 第一轮结束，u3 仍保留（不被清掉）
        assert len(c._buffer) == 1  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_storage_failure_requeues_batch(self):
        """B5/P1-11：存储失败（insert 抛错）→ batch 重新入队待重试，不静默丢。"""
        class OkLLM:
            async def think(self, messages, extra_body=None):
                class R:
                    text = '[{"type": "Fact", "content": "x", "poignancy": 5}]'
                return R()

        class FailingMemory:
            def get_all(self):
                return []  # 无已有记忆 → 直接 insert 路径（对齐 LongTermMemory 同步接口）

            async def insert(self, content, **kw):
                raise RuntimeError("storage down")

            async def search(self, **kw):
                raise AssertionError("不应搜索")

            async def update_text(self, *a, **kw):
                raise AssertionError("不应 update")

        c = MemoryConsolidator(OkLLM(), FailingMemory(), "Bot", buffer_threshold=1)  # type: ignore[arg-type]
        items = await c.consolidate("u", "a")
        assert items == []
        assert len(c._buffer) == 1  # batch 已 requeue（待重试）  # noqa: SLF001


# ============================================================================
# 4. MemoryConsolidator edge cases
# ============================================================================

@pytest.mark.service
class TestMemoryConsolidatorEdgeCases:
    @pytest.mark.asyncio
    async def test_consolidate_stores_in_memory(self):
        """整合应真正存储提取到的事实。"""
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
        """应能优雅处理达到字符上限的消息。"""
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


# ============================================================================
# 6. 辅助 prompt 参数化（B-2 Task 5）— 无 Elysia/妖梦
# ============================================================================

@pytest.mark.unit
class TestPromptPlaceholder:
    """B-2 Task 5：ConsolidationCompare/ExtractMemory prompt 无 Elysia/妖梦。"""

    def test_consolidation_prompt_no_elysia(self):
        """ConsolidationCompare/ExtractMemory prompt 无 Elysia/妖梦。"""
        for p in [_get_compare_prompt(), _get_extraction_prompt()]:
            assert "Elysia" not in p and "妖梦" not in p, "consolidation prompt 仍含 Elysia/妖梦"


class _FakeLLM:
    """最小 MemoryLLM 桩——仅用于构造 MemoryConsolidator，不触发提取。"""

    model = "mock"

    async def think(self, messages, **kw):
        return SimpleNamespace(text="[]")  # 无提取

    async def test(self):
        return {"ok": True, "latency_ms": 0}

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_consolidator_accepts_scoped_view():
    """consolidator 应接受 ScopedLongTermMemory（类型放宽为 union）。"""
    ltm = LongTermMemory(enable_eviction=False)
    view = ltm.for_scope(MemoryScope(user_id="usr_a", character_instance_id="chi_a"))
    cons = MemoryConsolidator(_FakeLLM(), memory=view)  # 应接受 scoped view
    assert cons._memory is view

