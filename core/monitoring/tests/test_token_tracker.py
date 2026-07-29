"""TokenTracker 单元测试。"""

import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock
import pytest
from core.monitoring.token_tracker import TokenTracker, TokenUsage
from core.schema.token import TokenUsageEntry
from core.schema.types import SessionId

pytestmark = pytest.mark.unit


class TestTokenUsage:
    """TokenUsage 数据类字段测试。"""

    def test_dataclass_fields(self):
        u = TokenUsage(
            model="deepseek/deepseek-chat",
            prompt_tokens=800,
            completion_tokens=200,
            total_tokens=1000,
            round_num=0,
            timestamp=1717718400.0,
        )
        assert u.model == "deepseek/deepseek-chat"
        assert u.total_tokens == 1000
        assert u.round_num == 0
        assert u.prompt_tokens == 800
        assert u.completion_tokens == 200
        assert u.timestamp == 1717718400.0


class TestTokenTracker:
    """TokenTracker 核心逻辑测试。"""

    @pytest.fixture
    def output_mock(self):
        output = MagicMock()
        output.token_usage = AsyncMock()
        return output

    @pytest.fixture
    def tracker(self, output_mock):
        return TokenTracker(
            session_id=SessionId("test-session"),
            output_manager=output_mock,
            repo=None,
        )

    # ── record ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_record_accumulates_session_total(self, tracker):
        await tracker.record(
            usage={"prompt_tokens": 800, "completion_tokens": 200, "total_tokens": 1000},
            model="deepseek/deepseek-chat",
            round_num=0,
        )
        assert tracker.session_total == 1000
        assert tracker.per_model == {"deepseek/deepseek-chat": 1000}

    @pytest.mark.asyncio
    async def test_record_per_model_separate(self, tracker):
        await tracker.record(
            usage={"prompt_tokens": 500, "completion_tokens": 500, "total_tokens": 1000},
            model="deepseek/deepseek-chat", round_num=0,
        )
        await tracker.record(
            usage={"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200},
            model="openai/gpt-4o", round_num=0,
        )
        assert tracker.session_total == 1200
        assert tracker.per_model == {
            "deepseek/deepseek-chat": 1000,
            "openai/gpt-4o": 200,
        }

    @pytest.mark.asyncio
    async def test_record_broadcasts_token_usage(self, tracker):
        await tracker.record(
            usage={"prompt_tokens": 800, "completion_tokens": 200, "total_tokens": 1000},
            model="deepseek/deepseek-chat", round_num=0,
        )
        tracker._output.token_usage.assert_awaited_once()
        call_data = tracker._output.token_usage.call_args[0][0]
        assert call_data["turn"]["turn_total"] == 1000
        assert call_data["session"]["total"] == 1000
        assert call_data["session"]["by_model"]["deepseek/deepseek-chat"] == 1000

    @pytest.mark.asyncio
    async def test_record_multiple_calls_in_same_turn(self, tracker):
        """同一轮内多次 record，turn.rounds 应包含多条记录。"""
        await tracker.record(
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            model="deepseek/deepseek-chat", round_num=0,
        )
        await tracker.record(
            usage={"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280},
            model="deepseek/deepseek-chat", round_num=1,
        )
        # 第二次广播应包含 2 条 round
        call_data = tracker._output.token_usage.call_args[0][0]
        assert len(call_data["turn"]["rounds"]) == 2
        assert call_data["turn"]["turn_total"] == 150 + 280
        assert call_data["session"]["total"] == 150 + 280

    # ── start_turn ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_start_turn_clears_current_usages(self, tracker):
        await tracker.record(
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            model="deepseek/deepseek-chat", round_num=0,
        )
        assert len(tracker.current_turn_usages) == 1
        tracker.start_turn()
        assert len(tracker.current_turn_usages) == 0
        assert tracker.session_total == 150  # session_total 不受影响

    # ── persist (JSONL fallback) ────────────────────────────

    @pytest.mark.asyncio
    async def test_persist_writes_jsonl_when_no_db(self, tracker):
        """没有 MongoDB 时，持久化到本地 JSONL 文件。"""
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".jsonl") as f:
            tracker._pending_file = f.name

        try:
            await tracker.record(
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                model="test/model", round_num=0,
            )
            with open(tracker._pending_file) as f:
                lines = f.readlines()
            assert len(lines) == 1
            record = json.loads(lines[0])
            assert record["model"] == "test/model"
            assert record["total_tokens"] == 15
            assert record["session_id"] == "test-session"
        finally:
            os.unlink(tracker._pending_file)

    @pytest.mark.asyncio
    async def test_persist_appends_multiple_records(self, tracker):
        """多次 record 应追加到同一 JSONL 文件，不覆盖。"""
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".jsonl") as f:
            tracker._pending_file = f.name

        try:
            await tracker.record(
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                model="a", round_num=0,
            )
            await tracker.record(
                usage={"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
                model="b", round_num=0,
            )
            with open(tracker._pending_file) as f:
                lines = f.readlines()
            assert len(lines) == 2
        finally:
            os.unlink(tracker._pending_file)

    # ── persist (MongoDB via repo) ──────────────────────────

    @pytest.mark.asyncio
    async def test_persist_to_mongodb_when_available(self, output_mock):
        """有 MongoDB 时，通过 TokenRepository 写入。"""
        repo_mock = MagicMock()
        repo_mock.record = AsyncMock()
        tracker = TokenTracker(
            session_id=SessionId("s1"), output_manager=output_mock, repo=repo_mock,
        )

        await tracker.record(
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            model="test/model", round_num=0,
        )
        repo_mock.record.assert_awaited_once()
        entry = repo_mock.record.call_args[0][0]
        assert entry.session_id == "s1"
        assert entry.total_tokens == 15

    @pytest.mark.asyncio
    async def test_persist_falls_back_to_jsonl_when_mongodb_fails(self, output_mock):
        """MongoDB 写入失败时，降级到 JSONL 文件。"""
        repo_mock = MagicMock()
        repo_mock.record = AsyncMock(side_effect=RuntimeError("connection lost"))
        tracker = TokenTracker(
            session_id=SessionId("s1"), output_manager=output_mock, repo=repo_mock,
        )

        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".jsonl") as f:
            tracker._pending_file = f.name

        try:
            await tracker.record(
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                model="fallback/model", round_num=0,
            )
            repo_mock.record.assert_awaited_once()
            with open(tracker._pending_file) as f:
                lines = f.readlines()
            assert len(lines) == 1
            record = json.loads(lines[0])
            assert record["model"] == "fallback/model"
            assert record["session_id"] == "s1"
        finally:
            os.unlink(tracker._pending_file)

    # ── flush_pending ───────────────────────────────────────

    @pytest.mark.asyncio
    async def test_flush_pending_imports_and_deletes_file(self, output_mock):
        """flush_pending 读取 JSONL 内容写入 MongoDB 并删除文件。"""
        repo_mock = MagicMock()
        repo_mock.flush = AsyncMock()
        tracker = TokenTracker(
            session_id=SessionId("s1"), output_manager=output_mock, repo=repo_mock,
        )

        # 写 TokenUsageEntry 兼容格式的 JSONL
        entry = TokenUsageEntry(
            session_id=SessionId("s1"), model="m",
            prompt_tokens=1, completion_tokens=1, total_tokens=2,
            round_num=0,
        )
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".jsonl") as f:
            f.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
            tracker._pending_file = f.name

        try:
            await tracker.flush_pending()
            repo_mock.flush.assert_awaited_once()
            entries = repo_mock.flush.call_args[0][0]
            assert len(entries) == 1
            assert entries[0].session_id == "s1"
            assert not os.path.exists(tracker._pending_file)
        finally:
            if os.path.exists(tracker._pending_file):
                os.unlink(tracker._pending_file)

    @pytest.mark.asyncio
    async def test_flush_pending_no_file_is_noop(self, output_mock):
        """pending 文件不存在时不执行任何操作。"""
        repo_mock = MagicMock()
        repo_mock.flush = AsyncMock()
        tracker = TokenTracker(
            session_id=SessionId("s1"), output_manager=output_mock, repo=repo_mock,
        )
        tracker._pending_file = "/tmp/nonexistent_token_test_file.jsonl"
        await tracker.flush_pending()
        repo_mock.flush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_flush_pending_no_db_is_noop(self, output_mock):
        """没有 MongoDB 时 flush 不执行任何操作。"""
        tracker = TokenTracker(
            session_id=SessionId("s1"), output_manager=output_mock, repo=None,
        )
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".jsonl") as f:
            f.write(json.dumps({"a": 1}) + "\n")
            tracker._pending_file = f.name

        try:
            await tracker.flush_pending()
            # 文件应该还在（没有 db 不能 flush）
            assert os.path.exists(tracker._pending_file)
        finally:
            if os.path.exists(tracker._pending_file):
                os.unlink(tracker._pending_file)

    # ── get_session_stats ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_session_stats_from_memory(self, tracker):
        """无 MongoDB 时，从内存返回统计数据。"""
        await tracker.record(
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            model="deepseek/deepseek-chat", round_num=0,
        )
        stats = await tracker.get_session_stats()
        assert stats["total"] == 150
        assert stats["by_model"] == {"deepseek/deepseek-chat": 150}

    @pytest.mark.asyncio
    async def test_get_session_stats_from_mongodb(self, output_mock):
        """有 MongoDB 时，从 TokenRepository 聚合查询 + pending 文件补充。"""
        repo_mock = MagicMock()
        repo_mock.total_tokens_by_model = AsyncMock(return_value={
            "deepseek/deepseek-chat": 50,
            "openai/gpt-4o": 50,
        })
        tracker = TokenTracker(
            session_id=SessionId("s1"), output_manager=output_mock, repo=repo_mock,
        )
        tracker._pending_file = "/tmp/test_stats_s1_nonexistent.jsonl"  # 隔离 pending 文件

        stats = await tracker.get_session_stats()
        assert stats["total"] == 100
        assert stats["by_model"] == {
            "deepseek/deepseek-chat": 50,
            "openai/gpt-4o": 50,
        }

    @pytest.mark.asyncio
    async def test_get_session_stats_mongodb_failure_falls_back_to_memory(self, output_mock):
        """MongoDB 查询失败时回退到内存数据。"""
        repo_mock = MagicMock()
        repo_mock.total_tokens_by_model = AsyncMock(side_effect=RuntimeError("db down"))
        tracker = TokenTracker(
            session_id=SessionId("s1"), output_manager=output_mock, repo=repo_mock,
        )

        # 先在内存累计一些数据
        await tracker.record(
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            model="test/model", round_num=0,
        )

        stats = await tracker.get_session_stats()
        assert stats["total"] == 15
        assert stats["by_model"] == {"test/model": 15}

    @pytest.mark.asyncio
    async def test_get_session_stats_includes_pending_file(self, output_mock):
        """MongoDB + pending 文件数据合并。"""
        repo_mock = MagicMock()
        repo_mock.total_tokens_by_model = AsyncMock(return_value={"a": 100})
        tracker = TokenTracker(
            session_id=SessionId("s1"), output_manager=output_mock, repo=repo_mock,
        )

        # 写 TokenUsageEntry 兼容格式的 pending 数据
        pending_entry = TokenUsageEntry(
            session_id=SessionId("s1"), model="a",
            prompt_tokens=10, completion_tokens=10, total_tokens=20, round_num=0,
        )
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".jsonl") as f:
            f.write(json.dumps(pending_entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
            tracker._pending_file = f.name

        try:
            stats = await tracker.get_session_stats()
            assert stats["total"] == 120  # 100 (db) + 20 (pending)
            assert stats["by_model"]["a"] == 120
        finally:
            os.unlink(tracker._pending_file)


@pytest.mark.unit
class TestTokenTrackerClearAll:
    @pytest.mark.asyncio
    async def test_clear_all_calls_repo_clear_all(self):
        repo_mock = MagicMock()
        repo_mock.clear_all = AsyncMock(return_value=42)
        deleted = await TokenTracker.clear_all(repo_mock)
        assert deleted == 42
        repo_mock.clear_all.assert_awaited_once()
