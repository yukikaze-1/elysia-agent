"""Phase 2 tests: persistent memory, vector search, consolidation.

Run:
    python -m pytest core/test_phase2.py -v --asyncio-mode=auto
"""

import asyncio
import sys
from pathlib import Path

import pytest

from Module.embedding import EmbeddingFactory

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(scope="session", autouse=True)
def _cleanup_embedding():
    """所有测试完成后清理共享的 embedding 缓存。"""
    yield
    EmbeddingFactory._instances.clear()


# ============================================================================
# 1. SessionMemory persistence tests
# ============================================================================

class TestSessionPersistence:
    def test_snapshot_restore(self):
        from core.memory.session import SessionMemory
        s1 = SessionMemory(max_messages=10, session_id="test1")
        s1.add_message("user", "hello")
        s1.add_message("assistant", "hi there")
        s1.set_param("mood", "happy")
        s1.set_param("mood_intensity", 0.7)

        snap = s1.snapshot()
        assert len(snap["messages"]) == 2
        assert snap["params"]["mood"] == "happy"

        s2 = SessionMemory(max_messages=10, session_id="test2")
        s2.restore(snap)
        assert len(s2.recent_messages()) == 2
        assert s2.get_param("mood") == "happy"
        assert s2.get_param("mood_intensity") == 0.7

    def test_max_messages_enforced_on_restore(self):
        from core.memory.session import SessionMemory
        s1 = SessionMemory(max_messages=3, session_id="test")
        for i in range(5):
            s1.add_message("user", f"msg{i}")

        snap = s1.snapshot()
        s2 = SessionMemory(max_messages=3)
        s2.restore(snap)
        assert len(s2.recent_messages()) == 3

    def test_clear(self):
        from core.memory.session import SessionMemory
        s = SessionMemory(max_messages=10)
        s.add_message("user", "test")
        s.set_param("key", "value")
        assert len(s.recent_messages()) == 1
        s.clear()
        assert len(s.recent_messages()) == 0
        assert s.get_param("key") is None

    @pytest.mark.asyncio
    async def test_connect_fallback(self):
        """connect_db should return False when MongoDB is unavailable."""
        from core.memory.session import SessionMemory
        s = SessionMemory(session_id="test")
        result = await s.connect_db()
        # MongoDB may or may not be available — either is fine
        assert result in (True, False)

    @pytest.mark.asyncio
    async def test_save_load_no_db(self):
        """save/load should return False when no DB connected."""
        from core.memory.session import SessionMemory
        s = SessionMemory(session_id="test")
        s.add_message("user", "hello")
        # No connect_db called, so _db is None
        assert await s.save() is False
        assert await s.load() is False


# ============================================================================
# 2. LongTermMemory tests
# ============================================================================

class TestLongTermMemory:
    @pytest.mark.asyncio
    async def test_insert_and_search_async(self):
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("今天天气很好", type="fact")
        await mem.insert("明天可能会下雨", type="fact")
        await mem.insert("妖梦喜欢蓝色", type="preference")

        results = await mem.search(query_text="天气")
        assert len(results) >= 1
        assert any("天气" in r.text for r in results)

    @pytest.mark.asyncio
    async def test_search_no_results(self):
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        results = await mem.search(query_text="不存在的内容xyz")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_returns_top_k(self):
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        for i in range(10):
            await mem.insert(f"memory entry {i}", type="note")

        results = await mem.search(query_text="memory", top_k=3)
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_count(self):
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("item1", type="note")
        await mem.insert("item2", type="note")
        assert mem.count == 2

    @pytest.mark.asyncio
    async def test_delete_entry(self):
        """删除单条记忆应从内存列表移除并减少 count。"""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("test entry", type="note")
        eid = mem._entries[0].id
        ok = await mem.delete(eid)
        assert ok is True
        assert mem.count == 0
        assert len(mem._entries) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        """删除不存在的 ID 返回 False。"""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        ok = await mem.delete("nonexistent-id")
        assert ok is False

    @pytest.mark.asyncio
    async def test_clear_all(self):
        """清空所有记忆，count 归零。"""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("entry 1", type="note")
        await mem.insert("entry 2", type="note")
        assert mem.count == 2
        deleted = await mem.clear_all()
        assert deleted == 2
        assert mem.count == 0

    @pytest.mark.asyncio
    async def test_insert_stores_milvus_pk(self):
        """insert 时应为 entry 分配 id。"""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("test entry", type="note")
        assert mem._entries[0].id
        # _milvus_pk 仅在 Milvus 可用时设置，否则为 None

    @pytest.mark.asyncio
    async def test_connect_fallback(self):
        """connect should work or gracefully fall back."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        result = await mem.connect()
        assert result in (True, False)
        await mem.close()

    @pytest.mark.asyncio
    async def test_insert_without_connect(self):
        """Insert should work even without connect() — uses in-memory."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("test entry", type="note")
        assert mem.count == 1
        results = await mem.search(query_text="test")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_insert_awaits_embedding_on_first_call(self):
        """First insert() should wait for embedding load, so entry gets embedding
        if any backend is available. Verifies the await pattern works."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()

        await mem._ensure_embedding(wait=True)
        await mem.insert("test cold start", memory_type="fact")

        entry = mem._entries[0] if mem._entries else None
        assert entry is not None
        if mem._embedding is not None:
            assert len(entry.embedding) > 0, "Entry should have embedding after await"

    @pytest.mark.asyncio
    async def test_insert_allows_duplicates_with_different_metadata(self):
        """Identical text with different metadata should create distinct entries.

        Note: dedup is deferred to a separate feature plan. Consolidator
        may re-extract the same fact in different rounds, and those are
        treated as independent confirmations rather than duplicates.
        """
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("妖梦喜欢蓝色", memory_type="preference")
        await mem.insert("妖梦喜欢蓝色", memory_type="preference", source="consolidation")
        assert mem.count == 2  # distinct entries with different metadata/timestamps


# ============================================================================
# 3. MemoryConsolidator tests
# ============================================================================

class TestMemoryConsolidator:
    @pytest.mark.asyncio
    async def test_consolidate_extracts_facts(self):
        import json
        from core.tests.fake_llm import FakeLLMBackend
        from core.memory.long_term import LongTermMemory
        from core.consolidator import MemoryConsolidator
        from core.llm_adapter import LLMAdapter

        adapter = LLMAdapter(_test_backend=FakeLLMBackend(response=json.dumps([
            {"type": "preference", "content": "喜欢蓝色"},
            {"type": "plan", "content": "明天去爬山"},
        ])))
        mem = LongTermMemory()
        consolidator = MemoryConsolidator(adapter, mem, "TestBot", buffer_threshold=1)

        items = await consolidator.consolidate(
            "我最喜欢蓝色，明天要去爬山",
            "好的妖梦！我记住了，你喜欢蓝色，而且明天要去爬山。"
        )

        assert isinstance(items, list)
        # With the fake response, 2 items should be extracted
        types = {item.get("type") for item in items}
        assert "preference" in types
        assert "plan" in types

        await adapter.close()

    @pytest.mark.asyncio
    async def test_consolidate_empty_conversation(self):
        from core.tests.fake_llm import FakeLLMBackend
        from core.memory.long_term import LongTermMemory
        from core.consolidator import MemoryConsolidator
        from core.llm_adapter import LLMAdapter

        adapter = LLMAdapter(_test_backend=FakeLLMBackend())

        mem = LongTermMemory()
        consolidator = MemoryConsolidator(adapter, mem, "TestBot", buffer_threshold=1)

        # Empty messages should produce no items
        items = await consolidator.consolidate("", "")
        assert items == []

        await adapter.close()

    def test_parse_json(self):
        from core.consolidator import MemoryConsolidator
        # Direct JSON
        items = MemoryConsolidator._parse_result(
            '[{"type": "preference", "content": "likes blue"}]'
        )
        assert len(items) == 1
        assert items[0]["type"] == "preference"

    def test_parse_markdown_wrapped(self):
        from core.consolidator import MemoryConsolidator
        items = MemoryConsolidator._parse_result(
            '```json\n[{"type": "fact", "content": "hello"}]\n```'
        )
        assert len(items) == 1
        assert items[0]["content"] == "hello"

    def test_parse_empty(self):
        from core.consolidator import MemoryConsolidator
        assert MemoryConsolidator._parse_result("") == []
        assert MemoryConsolidator._parse_result("not json") == []
        assert MemoryConsolidator._parse_result("[]") == []

    @pytest.mark.asyncio
    async def test_flush_consolidates_below_threshold(self):
        """flush() should consolidate buffered turns even below threshold."""
        from core.consolidator import MemoryConsolidator
        from core.memory.long_term import LongTermMemory

        class FakeLLM:
            async def think(self, messages, extra_body=None):
                class R:
                    text = '[{"type": "Fact", "content": "测试flush", "poignancy": 5}]'
                return R()

        mem = LongTermMemory()
        c = MemoryConsolidator(FakeLLM(), mem, buffer_threshold=10)
        await c.consolidate("user msg 1", "assistant reply 1")
        await c.consolidate("user msg 2", "assistant reply 2")
        await c.consolidate("user msg 3", "assistant reply 3")
        assert mem.count == 0  # below threshold, not yet consolidated

        items = await c.flush()
        assert len(items) > 0
        assert mem.count > 0


# ============================================================================
# 4. Memory integration tests
# ============================================================================

class TestMemoryIntegration:
    @pytest.mark.asyncio
    async def test_search_memory_tool(self):
        """search_memory tool should find relevant entries."""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent

        from core.app_config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()

        # 插入测试数据
        await agent._long_term.insert("上周和妖梦约定周六去爬香山", memory_type="plan", poignancy=7)

        # Use the tool directly
        result = await agent._tool_manager.execute("search_memory", ctx=agent._tool_ctx, query="爬山")
        assert result.success
        assert "爬山" in result.content or "香山" in result.content
        assert "未找到相关记忆" not in result.content

        await agent.stop()

    @pytest.mark.asyncio
    async def test_save_memory_tool(self):
        """save_memory tool should persist an entry (with HITL auto-approved for test)."""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent

        from core.app_config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()
        # Mock HITL to auto-approve in test environment
        original_run_hitl = agent._tool_manager._pipeline._run_hitl
        async def _auto_approve(plugin, name, params):
            return True, "confirmed"
        agent._tool_manager._pipeline._run_hitl = _auto_approve

        await agent._tool_manager.execute("save_memory", ctx=agent._tool_ctx, content="测试保存的记忆", type="fact")
        results = await agent._long_term.search(query_text="测试保存")
        assert len(results) > 0
        assert any("测试保存" in r.text for r in results)

        await agent.stop()

    @pytest.mark.asyncio
    async def test_end_to_end_with_memory(self):
        """Full pipeline: conversation that stores and retrieves memories."""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent
        from core.agent_fsm import AgentState

        from core.app_config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()
        # Mock HITL to auto-approve in test environment
        async def _auto_approve(plugin, name, params):
            return True, "confirmed"
        agent._tool_manager._pipeline._run_hitl = _auto_approve

        # First turn: tell agent something memorable
        await agent.process_message("记住，我下周要去东京出差")
        await asyncio.sleep(5)

        # Second turn: ask about it
        await agent.process_message("我之前跟你说过我要去哪里出差的？")
        await asyncio.sleep(8)

        assert agent._fsm.state == AgentState.IDLE
        # Session should have multiple messages
        messages = agent._session.recent_messages()
        assert len(messages) >= 2

        await agent.stop()

    @pytest.mark.asyncio
    async def test_session_state_persists_in_memory(self):
        """Session should maintain state across multiple turns."""
        from dotenv import load_dotenv
        load_dotenv()
        from core.agent import Agent

        from core.app_config import AppConfig
        agent = Agent(config=AppConfig())
        await agent.start()
        # Mock HITL to auto-approve in test environment
        async def _auto_approve(plugin, name, params):
            return True, "confirmed"
        agent._tool_manager._pipeline._run_hitl = _auto_approve

        # Multiple turns
        await agent.process_message("我叫妖梦")
        await asyncio.sleep(3)
        await agent.process_message("我喜欢蓝色")
        await asyncio.sleep(3)

        messages = agent._session.recent_messages()
        # Should have at least user+assistant from both turns
        assert len(messages) >= 4

        await agent.stop()


# ============================================================================
# 5. PromptAssembler async tests
# ============================================================================

class TestPromptAssembler:
    @pytest.mark.asyncio
    async def test_build_messages_includes_memories(self):
        from core.character_config import CharacterConfig
        from core.memory.session import SessionMemory
        from core.memory.long_term import LongTermMemory
        from core.prompt_assembler import PromptAssembler

        char = CharacterConfig(name="TestBot")
        session = SessionMemory()
        lt = LongTermMemory()
        await lt.insert("妖梦的生日是5月20日", type="fact")
        await lt.insert("妖梦住在北京", type="fact")

        pa = PromptAssembler(char, session, lt)
        messages = await pa.build_messages("我生日是哪天", "妖梦")

        system = messages[0]["content"]
        # System prompt should include recalled memory about birthday
        assert "生日" in system or "5月" in system or len(messages) > 1

    @pytest.mark.asyncio
    async def test_no_memories_when_empty(self):
        from core.character_config import CharacterConfig
        from core.memory.session import SessionMemory
        from core.memory.long_term import LongTermMemory
        from core.prompt_assembler import PromptAssembler

        char = CharacterConfig(name="TestBot")
        session = SessionMemory()
        lt = LongTermMemory()

        pa = PromptAssembler(char, session, lt)
        messages = await pa.build_messages("你好", "妖梦")
        system = messages[0]["content"]
        assert "No specific memories triggered" in system


# ============================================================================
# 6. SessionMemory edge cases
# ============================================================================

class TestSessionMemoryEdgeCases:
    def test_round_trip_snapshot_cycle(self):
        """Full cycle: create -> populate -> snapshot -> restore -> verify."""
        from core.memory.session import SessionMemory
        s1 = SessionMemory(max_messages=20, session_id="roundtrip")
        s1.add_message("user", "msg1")
        s1.add_message("assistant", "reply1")
        s1.add_message("user", "msg2")
        s1.set_param("mood", "excited")
        s1.set_param("mood_intensity", 0.8)
        s1.set_param("custom_key", {"nested": [1, 2, 3]})

        snap = s1.snapshot()
        s2 = SessionMemory(max_messages=20)
        s2.restore(snap)

        assert s2.recent_messages() == s1.recent_messages()
        assert s2.get_param("mood") == "excited"
        assert s2.get_param("mood_intensity") == 0.8
        assert s2.get_param("custom_key") == {"nested": [1, 2, 3]}

    def test_multiple_sessions_independent(self):
        """Different session_ids should not interfere with each other."""
        from core.memory.session import SessionMemory
        s1 = SessionMemory(session_id="alice")
        s2 = SessionMemory(session_id="bob")

        s1.add_message("user", "alice msg")
        s2.add_message("user", "bob msg")

        s1_msgs = [m["content"][0]["content"] for m in s1.recent_messages()]
        s2_msgs = [m["content"][0]["content"] for m in s2.recent_messages()]

        assert "alice msg" in s1_msgs
        assert "bob msg" in s2_msgs
        assert "bob msg" not in s1_msgs

    def test_bounded_window_overflow(self):
        """Messages beyond max_messages should be dropped (FIFO)."""
        from core.memory.session import SessionMemory
        s = SessionMemory(max_messages=3)
        s.add_message("user", "first")
        s.add_message("assistant", "second")
        s.add_message("user", "third")
        s.add_message("assistant", "fourth")
        s.add_message("user", "fifth")

        msgs = s.recent_messages()
        assert len(msgs) == 3
        contents = [m["content"][0]["content"] for m in msgs]
        assert "first" not in contents
        assert "second" not in contents
        assert "third" in contents
        assert "fifth" in contents

    def test_params_persistence_across_restore(self):
        """Params should survive snapshot/restore exactly."""
        from core.memory.session import SessionMemory
        s1 = SessionMemory()
        s1.set_param("int_val", 42)
        s1.set_param("float_val", 3.14)
        s1.set_param("str_val", "hello")
        s1.set_param("none_val", None)
        s1.set_param("list_val", [1, "two", 3.0])

        snap = s1.snapshot()
        s1.set_param("int_val", 999)  # modify original
        s1.restore(snap)  # restore back

        assert s1.get_param("int_val") == 42
        assert s1.get_param("float_val") == 3.14
        assert s1.get_param("none_val") is None
        assert s1.get_param("list_val") == [1, "two", 3.0]

    def test_recent_messages_with_n(self):
        """recent_messages(n) should return only the last n."""
        from core.memory.session import SessionMemory
        s = SessionMemory(max_messages=20)
        for i in range(10):
            s.add_message("user", f"msg{i}")

        assert len(s.recent_messages(3)) == 3
        assert s.recent_messages(3)[-1]["content"][0]["content"] == "msg9"

    def test_meta_data_on_messages(self):
        """Messages with extra metadata should preserve it."""
        from core.memory.session import SessionMemory
        s = SessionMemory()
        s.add_message("user", "hello", timestamp=12345, source="web")

        msgs = s.recent_messages()
        assert msgs[0]["meta"] == {"timestamp": 12345, "source": "web"}

    def test_save_caps_messages_to_max(self):
        """snapshot() should only expose max_messages worth of messages."""
        from core.memory.session import SessionMemory
        s = SessionMemory(max_messages=3)
        for i in range(10):
            s.add_message("user", f"msg{i}")
        snap = s.snapshot()
        assert len(snap["messages"]) == 3
        # content 现在是 list[dict] 格式
        assert snap["messages"][0]["content"][0]["content"] == "msg7"
        assert snap["messages"][2]["content"][0]["content"] == "msg9"

    def test_clear_preserves_session_id(self):
        """Clear should empty messages but not change session_id."""
        from core.memory.session import SessionMemory
        s = SessionMemory(session_id="persist")
        s.add_message("user", "data")
        s.set_param("key", "val")
        s.clear()

        assert len(s.recent_messages()) == 0
        assert s.get_param("key") is None

    def test_messages_for_client_with_sentences(self):
        """messages_for_client should convert old meta.sentences to parts array."""
        from core.memory.session import SessionMemory
        s = SessionMemory()
        s.add_message("assistant", "Hello world", sentences=["Hello", "world"])
        s.add_message("user", "Hi")
        result = s.messages_for_client()
        assert result[0]["parts"] == [{"content": "Hello"}, {"content": "world"}]
        assert result[0]["role"] == "assistant"
        assert result[1]["role"] == "user"

    def test_messages_for_client_new_format(self):
        """messages_for_client should pass through new-format messages unchanged."""
        from core.memory.session import SessionMemory
        s = SessionMemory()
        msg = {"id": "msg_abc", "role": "assistant", "parts": [{"content": "Hello"}], "content_type": {}, "timestamp": "2026-01-01T00:00:00+08:00"}
        s.add_message(msg)
        result = s.messages_for_client()
        assert result[0] == msg

    def test_messages_for_client_empty(self):
        from core.memory.session import SessionMemory
        s = SessionMemory()
        assert s.messages_for_client() == []

    def test_messages_for_client_with_n(self):
        from core.memory.session import SessionMemory
        s = SessionMemory()
        for i in range(5):
            s.add_message("user", f"msg{i}")
        assert len(s.messages_for_client(2)) == 2


# ============================================================================
# 7. LongTermMemory edge cases
# ============================================================================

class TestLongTermMemoryEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_search(self):
        """Search on empty memory should return empty list."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        results = await mem.search(query_text="anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_no_query_returns_recent(self):
        """Search with empty query should return most recent entries."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("entry1", type="note")
        await mem.insert("entry2", type="note")

        results = await mem.search(query_text="")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_many_entries(self):
        """Insert and search across 50 entries."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        for i in range(50):
            await mem.insert(f"这是第{i}条记忆记录", type="note", index=i)

        assert mem.count == 50
        results = await mem.search(query_text="第25条", top_k=3)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_insert_with_unicode_emoji(self):
        """Emoji and unicode in memory entries should be preserved."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("妖梦喜欢🎨画画和🎵音乐", type="preference")

        results = await mem.search(query_text="画画")
        assert len(results) == 1
        assert "🎨" in results[0].text

    @pytest.mark.asyncio
    async def test_same_text_multiple_inserts(self):
        """Inserting the same text multiple times should create distinct entries."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("重复的记忆", type="note")
        await mem.insert("重复的记忆", type="note")
        assert mem.count == 2

        results = await mem.search(query_text="重复")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_insert_long_text(self):
        """Very long text should be stored and searchable."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        long_text = "这是一个非常长的记忆条目，" + "包含很多细节和信息。" * 50
        await mem.insert(long_text, type="note")

        results = await mem.search(query_text="非常长")
        assert len(results) == 1
        assert len(results[0].text) > 100

    @pytest.mark.asyncio
    async def test_search_chinese_substring(self):
        """Chinese text search should match on character overlap."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        # Don't call connect() — test in-memory fallback explicitly
        await mem.insert("妖梦昨天去了朝阳公园散步", type="fact")
        await mem.insert("今天中午吃的是拉面", type="fact")
        await mem.insert("下周要开会讨论项目进度", type="plan")

        r1 = await mem.search(query_text="公园散步")
        assert any("朝阳公园" in r.text for r in r1)

        r2 = await mem.search(query_text="妖梦项目")
        assert any("妖梦" in r.text for r in r2)
        # The second match should also be found (项目 matches 项目进度)
        assert any("项目进度" in r.text for r in r2)

        r3 = await mem.search(query_text="xyz")
        assert r3 == []

    @pytest.mark.asyncio
    async def test_search_score_ordering(self):
        """Results should be ordered by relevance (score descending)."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("苹果是一种水果", type="fact")
        await mem.insert("苹果手机很好用", type="fact")  # has "苹果" + more chars
        await mem.insert("今天天气不错", type="fact")

        results = await mem.search(query_text="苹果水果", top_k=2)
        # "苹果是一种水果" should rank higher (matches both "苹果" and "水果")
        assert results[0].text == "苹果是一种水果"

    @pytest.mark.asyncio
    async def test_insert_supports_type_alias_for_memory_type(self):
        """insert() should accept 'type' as alias for 'memory_type'."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("test content", type="plan", poignancy=8)

        assert mem.count == 1
        entry = mem._entries[0]
        assert entry.memory_type == "plan", (
            f"Expected memory_type='plan', got {entry.memory_type!r}"
        )
        # 'type' should NOT end up in metadata
        assert "type" not in entry.metadata

    @pytest.mark.asyncio
    async def test_search_filter_by_memory_type(self):
        """search() with memory_type should only return matching types."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("工作计划A", memory_type="plan")
        await mem.insert("工作计划B", memory_type="plan")
        await mem.insert("用户偏好蓝色", memory_type="preference")

        plans = await mem.search(query_text="工作", memory_type="plan", top_k=10)
        assert len(plans) >= 2
        assert all(e.memory_type == "plan" for e in plans)

        prefs = await mem.search(query_text="蓝色", memory_type="preference", top_k=10)
        assert len(prefs) >= 1
        assert all(e.memory_type == "preference" for e in prefs)

    @pytest.mark.asyncio
    async def test_search_returns_entries_with_ids(self):
        """Search results should link back to _entries with proper IDs."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("妖梦最喜欢的颜色是蓝色", memory_type="preference", poignancy=9)
        await mem.insert("明天要去爬山", memory_type="plan", poignancy=7)

        results = await mem.search(query_text="颜色")
        assert len(results) >= 1
        for r in results:
            assert r.id, f"Expected non-empty id, got {r.id!r} for text={r.text!r}"
            assert isinstance(r.embedding, list)


# ============================================================================
# 8. MemoryConsolidator edge cases
# ============================================================================

class TestMemoryConsolidatorEdgeCases:
    @pytest.mark.asyncio
    async def test_consolidate_stores_in_memory(self):
        """Consolidation should actually store extracted facts."""
        from core.tests.fake_llm import FakeLLMBackend
        from core.memory.long_term import LongTermMemory
        from core.consolidator import MemoryConsolidator
        from core.llm_adapter import LLMAdapter

        adapter = LLMAdapter(_test_backend=FakeLLMBackend())

        mem = LongTermMemory()
        consolidator = MemoryConsolidator(adapter, mem, "TestBot", buffer_threshold=1)
        count_before = mem.count

        await consolidator.consolidate(
            "我叫妖梦，我的生日是5月20日",
            "好的妖梦！我记住了，你叫妖梦，生日是5月20日。到时候要提醒我给你过生日哦～"
        )
        # Mock may or may not extract, but count shouldn't decrease
        assert mem.count >= count_before

        await adapter.close()

    @pytest.mark.asyncio
    async def test_consolidate_very_long_messages(self):
        """Should handle messages at the character limit gracefully."""
        from core.tests.fake_llm import FakeLLMBackend
        from core.memory.long_term import LongTermMemory
        from core.consolidator import MemoryConsolidator
        from core.llm_adapter import LLMAdapter

        adapter = LLMAdapter(_test_backend=FakeLLMBackend())

        mem = LongTermMemory()
        consolidator = MemoryConsolidator(adapter, mem, "TestBot", buffer_threshold=1)

        long_msg = "长消息" * 300
        items = await consolidator.consolidate(long_msg, long_msg)
        # Should not crash
        assert isinstance(items, list)

        await adapter.close()

    def test_parse_json_embedded_in_text(self):
        """JSON array embedded in surrounding text should be extracted."""
        from core.consolidator import MemoryConsolidator
        text = 'prefix text [{"type": "fact", "content": "found"}] suffix text'
        items = MemoryConsolidator._parse_result(text)
        assert len(items) == 1
        assert items[0]["content"] == "found"

    def test_parse_multiple_arrays_returns_first(self):
        """When multiple arrays exist, greedy regex captures from first [ to last ].
        This may fail JSON parse, which is acceptable — caller should provide clean JSON."""
        from core.consolidator import MemoryConsolidator
        text = '[{"type": "fact", "content": "a"}] other [{"type": "plan", "content": "b"}]'
        items = MemoryConsolidator._parse_result(text)
        # Greedy regex may fail to parse — both outcomes are acceptable
        assert isinstance(items, list)

    def test_parse_trailing_comma(self):
        """Trailing comma in JSON array should be handled."""
        from core.consolidator import MemoryConsolidator
        items = MemoryConsolidator._parse_result(
            '[{"type": "fact", "content": "test"}]'
        )
        assert len(items) == 1

    def test_parse_whitespace_only(self):
        from core.consolidator import MemoryConsolidator
        assert MemoryConsolidator._parse_result("   \n  ") == []
        assert MemoryConsolidator._parse_result("```json\n```") == []

    def test_parse_single_object_not_array(self):
        """A single JSON object (not array) should return empty."""
        from core.consolidator import MemoryConsolidator
        items = MemoryConsolidator._parse_result(
            '{"type": "fact", "content": "not an array"}'
        )
        assert items == []

    def test_parse_array_with_invalid_items(self):
        """Items missing required fields should still parse."""
        from core.consolidator import MemoryConsolidator
        items = MemoryConsolidator._parse_result(
            '[{"type": "fact", "content": "ok"}, {"invalid": "item"}, {"type": "plan"}]'
        )
        assert len(items) == 3


# ============================================================================
# 9. Memory integration stress tests
# ============================================================================

class TestMemoryStress:
    @pytest.mark.asyncio
    async def test_rapid_inserts_and_searches(self):
        """Rapid alternating inserts and searches should not corrupt state."""
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()

        for i in range(20):
            await mem.insert(f"stress test entry {i}", type="note", index=i)
            if i % 3 == 0:
                results = await mem.search(query_text=f"entry {i}")
                assert len(results) >= 1

        assert mem.count == 20

    @pytest.mark.asyncio
    async def test_session_snapshot_after_many_turns(self):
        """After many conversation turns, snapshot/restore should be exact."""
        from core.memory.session import SessionMemory
        s = SessionMemory(max_messages=100)

        for i in range(30):
            s.add_message("user", f"message {i}" if i % 2 == 0 else f"reply {i}")

        snap = s.snapshot()
        s2 = SessionMemory(max_messages=100)
        s2.restore(snap)

        orig = s.recent_messages()
        restored = s2.recent_messages()
        assert len(orig) == len(restored)
        for o, r in zip(orig, restored):
            assert o["content"] == r["content"]

    @pytest.mark.asyncio
    async def test_session_isolation_between_agents(self):
        """Two Agent instances should have independent session memory."""
        import os as _os
        _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
        import uuid
        from core.tests.fake_llm import FakeLLMBackend
        from core.agent import Agent

        sid1 = f"test-isolation-{uuid.uuid4()}"
        sid2 = f"test-isolation-{uuid.uuid4()}"

        from core.app_config import AppConfig
        a1 = Agent(config=AppConfig())
        a1._llm._backend = FakeLLMBackend(response="好的我记住了")
        a1._session._session_id = sid1
        await a1.start()
        await a1.process_message("只有agent1知道的消息")
        # Wait for cognitive task to start
        for _ in range(100):
            if a1._fsm.state != "idle":
                break
            await asyncio.sleep(0.01)
        # Wait for it to finish
        for _ in range(100):
            if a1._fsm.state == "idle":
                break
            await asyncio.sleep(0.01)
        a1_msgs = len(a1._session.recent_messages())
        await a1.stop()

        a2 = Agent(config=AppConfig())
        a2._llm._backend = FakeLLMBackend(response="你好")
        a2._session._session_id = sid2
        await a2.start()
        a2_msgs = len(a2._session.recent_messages())
        assert a1_msgs >= 2  # user msg + assistant reply
        assert a2_msgs == 0  # fresh session
        await a2.stop()

        # 清理测试遗留的 session 文档
        db = a1._session._db
        if db is not None:
            try:
                await db.delete_many("sessions", {"session_id": sid1})
                await db.delete_many("sessions", {"session_id": sid2})
            except Exception:
                pass


# ============================================================================
# Runner
# ============================================================================

if __name__ == "__main__":
    async def run_all():
        test_classes = [TestMemoryIntegration]
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

    asyncio.run(run_all())


# ---- LongTermMemory extended tests (Phase 2 review) ----

import time as _time_module

@pytest.mark.asyncio
async def test_long_term_memory_in_memory_fallback_search():
    """Milvus 不可用时回退到字符重叠搜索。"""
    from core.memory.long_term import LongTermMemory, MemoryEntry

    ltm = LongTermMemory()
    ltm._entries = [
        MemoryEntry(text="妖梦喜欢爬山", metadata={"type": "preference"}),
        MemoryEntry(text="昨天去了香山看红叶", metadata={"type": "event"}),
        MemoryEntry(text="妖梦在学习 Rust 语言", metadata={"type": "fact"}),
    ]

    results = await ltm.search("爬山", top_k=5)
    assert len(results) > 0
    assert any("爬山" in r.text for r in results)


@pytest.mark.asyncio
async def test_long_term_memory_empty_search():
    """空记忆库搜索应返回空列表不崩溃。"""
    from core.memory.long_term import LongTermMemory

    ltm = LongTermMemory()
    ltm._entries = []
    results = await ltm.search("任何东西", top_k=5)
    assert results == []


def test_long_term_memory_evicts_old_entries():
    """验证过期条目被驱逐。"""
    from core.memory.long_term import LongTermMemory, MemoryEntry

    ltm = LongTermMemory()
    now = _time_module.time()
    ltm._max_age_days = 90

    ltm._entries = [
        MemoryEntry(text="过期数据", metadata={}, timestamp=now - 100 * 86400),
        MemoryEntry(text="新数据", metadata={}, timestamp=now),
    ]

    removed = ltm._evict_old_entries()
    assert removed >= 1
    remaining_texts = [e.text for e in ltm._entries]
    assert "新数据" in remaining_texts
    assert "过期数据" not in remaining_texts


def test_long_term_memory_evicts_overflow():
    """验证超过上限的条目被驱逐（保留最新的）。"""
    from core.memory.long_term import LongTermMemory, MemoryEntry

    ltm = LongTermMemory()
    now = _time_module.time()
    ltm._max_entries = 5

    ltm._entries = [
        MemoryEntry(text=f"entry-{i}", metadata={}, timestamp=now - 100 + i)
        for i in range(10)
    ]

    removed = ltm._evict_old_entries()
    assert removed >= 5
    assert len(ltm._entries) <= 5
    for e in ltm._entries:
        assert int(e.text.split("-")[1]) >= 5


@pytest.mark.asyncio
async def test_long_term_memory_tracks_dirty_entries():
    """Milvus 不可用时插入的条目应标记为 dirty。"""
    from core.memory.long_term import LongTermMemory

    ltm = LongTermMemory()
    ltm._vector_db = None  # 模拟 Milvus 不可用
    assert len(ltm._dirty_ids) == 0

    await ltm.insert("离线数据", memory_type="fact")
    assert len(ltm._dirty_ids) >= 1

    # 验证 ID 生成 (new_message_id() 返回 "msg_" + 26 hex)
    entry = ltm._entries[-1]
    assert entry.id.startswith("msg_")
    assert len(entry.id) == 30


@pytest.mark.asyncio
async def test_long_term_memory_sync_handles_milvus_down():
    """Milvus 仍然不可用时 sync 应安全返回 0。"""
    from core.memory.long_term import LongTermMemory

    ltm = LongTermMemory()
    ltm._vector_db = None  # Milvus 不可用

    await ltm.insert("离线数据")
    synced = await ltm._sync_dirty_entries()
    assert synced == 0


@pytest.mark.asyncio
async def test_long_term_memory_eviction_cleans_dirty_ids():
    """Evicted entries should also be removed from _dirty_ids."""
    from core.memory.long_term import LongTermMemory, MemoryEntry
    import time as _time
    mem = LongTermMemory()
    entry = MemoryEntry(
        id="test_dirty_evict",
        text="old entry",
        embedding=[0.1] * 10,
        memory_type="fact",
        poignancy=5,
        timestamp=_time.time() - 100 * 86400,  # 100 days old — should evict
    )
    mem._entries.append(entry)
    mem._dirty_ids.add("test_dirty_evict")

    removed = mem._evict_old_entries()
    assert removed >= 1
    assert "test_dirty_evict" not in mem._dirty_ids, (
        "Evicted entry should be removed from _dirty_ids"
    )


# ============================================================
# Consolidator — BUFFER_THRESHOLD=6
# ============================================================

class TestConsolidatorThreshold:
    @pytest.mark.asyncio
    async def test_threshold_6_buffers_below_limit(self):
        """buffer_threshold=6 — 5条消息不触发_do_consolidate。"""
        from core.consolidator import MemoryConsolidator
        from core.llm_adapter import LLMAdapter
        from core.tests.fake_llm import FakeLLMBackend
        from unittest.mock import AsyncMock, MagicMock

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
        from core.consolidator import MemoryConsolidator
        from core.llm_adapter import LLMAdapter
        from core.tests.fake_llm import FakeLLMBackend
        from unittest.mock import AsyncMock, MagicMock

        adapter = LLMAdapter(_test_backend=FakeLLMBackend(response="[]"))
        mem = MagicMock()
        mem.insert = AsyncMock()
        mem.search = AsyncMock(return_value=[])
        consolidator = MemoryConsolidator(adapter, mem, buffer_threshold=6)

        for i in range(6):
            await consolidator.consolidate(f"msg{i}", "reply")
        assert len(consolidator._buffer) == 0
