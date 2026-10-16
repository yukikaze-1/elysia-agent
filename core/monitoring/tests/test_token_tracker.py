"""TokenTracker 单元测试。"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from core.monitoring.token_tracker import TokenTracker
from core.schema.token import TokenUsage
from core.schema.types import SessionId

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]


class TestTokenTracker:
    """TokenTracker 核心逻辑测试。"""

    @pytest.fixture
    def output_mock(self):
        output = MagicMock()
        output.token_usage = AsyncMock()
        return output

    @pytest.fixture
    def tracker(self, output_mock, tmp_path):
        t = TokenTracker(
            session_id=SessionId("test-session"),
            output_manager=output_mock,
            repo=None,
        )
        # 使用 tmp_path 避免污染项目 data/ 目录（pytest 自动清理）
        t._pending_file = str(tmp_path / "token_usage_pending.jsonl")
        return t

    # ── record ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_record_accumulates_session_total(self, tracker):
        await tracker.record(
            usage={"input_tokens": 800, "output_tokens": 200, "total_tokens": 1000},
            model="deepseek/deepseek-chat",
            round_num=0,
        )
        assert tracker.session_total == 1000
        assert tracker.per_model == {"deepseek/deepseek-chat": 1000}

    @pytest.mark.asyncio
    async def test_record_per_model_separate(self, tracker):
        await tracker.record(
            usage={"input_tokens": 500, "output_tokens": 500, "total_tokens": 1000},
            model="deepseek/deepseek-chat", round_num=0,
        )
        await tracker.record(
            usage={"input_tokens": 100, "output_tokens": 100, "total_tokens": 200},
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
            usage={"input_tokens": 800, "output_tokens": 200, "total_tokens": 1000},
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
            usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            model="deepseek/deepseek-chat", round_num=0,
        )
        await tracker.record(
            usage={"input_tokens": 200, "output_tokens": 80, "total_tokens": 280},
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
            usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
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
                usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
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
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                model="a", round_num=0,
            )
            await tracker.record(
                usage={"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
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
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
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
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
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

        # 写 TokenUsage 兼容格式的 JSONL
        entry = TokenUsage(
            session_id=SessionId("s1"), model="m",
            input_tokens=1, output_tokens=1, total_tokens=2,
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
    async def test_flush_pending_skips_legacy_lines(self, output_mock, tmp_path):
        """旧格式 pending 行跳过不导入，文件仍删除（tmp_path 隔离，不污染 data/）。"""
        repo_mock = AsyncMock()
        tracker = TokenTracker(
            session_id=SessionId("default"), output_manager=output_mock, repo=repo_mock,
        )
        tracker._pending_file = str(tmp_path / "token_usage_pending.jsonl")
        with open(tracker._pending_file, "w") as f:
            f.write(json.dumps({
                "session_id": "default", "model": "m",
                "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
                "round_num": 0, "timestamp": "2026-07-01T00:00:00+00:00",
            }) + "\n")
        await tracker.flush_pending()
        repo_mock.flush.assert_not_awaited()  # 无有效记录 → 不调用 flush
        assert not os.path.exists(tracker._pending_file)  # 整文件仍删除，旧行不保留

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
            usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
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
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
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

        # 写 TokenUsage 兼容格式的 pending 数据
        pending_entry = TokenUsage(
            session_id=SessionId("s1"), model="a",
            input_tokens=10, output_tokens=10, total_tokens=20, round_num=0,
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


    # ── 批次 3 multi-session：会话感知 ────────────────────────

    @pytest.mark.asyncio
    async def test_record_per_session_totals_independent(self, tracker):
        """不同 session_id 的记录累计相互独立，get_session_stats(sid) 按会话返回。"""
        await tracker.record(
            usage={"input_tokens": 800, "output_tokens": 200, "total_tokens": 1000},
            model="deepseek/deepseek-chat", round_num=0, session_id="a",
        )
        await tracker.record(
            usage={"input_tokens": 100, "output_tokens": 100, "total_tokens": 200},
            model="deepseek/deepseek-chat", round_num=0, session_id="a",
        )
        await tracker.record(
            usage={"input_tokens": 50, "output_tokens": 50, "total_tokens": 100},
            model="deepseek/deepseek-chat", round_num=0, session_id="b",
        )
        stats_a = await tracker.get_session_stats("a")
        stats_b = await tracker.get_session_stats("b")
        assert stats_a["total"] == 1200
        assert stats_b["total"] == 100
        # 独立：b 的累计不影响 a
        assert stats_a["by_model"]["deepseek/deepseek-chat"] == 1200
        assert stats_b["by_model"]["deepseek/deepseek-chat"] == 100

    @pytest.mark.asyncio
    async def test_record_broadcast_by_model_per_session(self, tracker):
        """批次 C：广播 session.by_model 按 sid（F8 只同步 total 未同步 per_model 的补齐）。

        修复前广播 `by_model: dict(self.per_model)` 是跨会话全局累计——total=最近
        会话、by_model=全局，前端 token_stats 显示其他会话模型累计，不一致。
        """
        await tracker.record(
            usage={"input_tokens": 800, "output_tokens": 200, "total_tokens": 1000},
            model="deepseek/deepseek-chat", round_num=0, session_id="a",
        )
        await tracker.record(
            usage={"input_tokens": 50, "output_tokens": 50, "total_tokens": 100},
            model="openai/gpt-4o", round_num=0, session_id="b",
        )
        # 最后一次广播：total=最近会话 b 的累计，by_model 也应只含 b 的模型
        call_data = tracker._output.token_usage.call_args[0][0]
        assert call_data["session"]["total"] == 100
        assert call_data["session"]["by_model"] == {"openai/gpt-4o": 100}
        assert "deepseek/deepseek-chat" not in call_data["session"]["by_model"], \
            "广播 by_model 不应含其他会话累计"
        # 兼容既有读取：self.per_model 属性仍保持全局累计（debug/runtime_config 不破）
        assert tracker.per_model["deepseek/deepseek-chat"] == 1000

    @pytest.mark.asyncio
    async def test_session_total_attribute_syncs_to_recent_session(self, tracker):
        """session_total 保持普通 int 属性，record 后同步为最近记录会话累计。

        debug/info.py（tracker.session_total）与 runtime_config（getattr+除0）读取方式不破。
        """
        await tracker.record(
            usage={"input_tokens": 800, "output_tokens": 200, "total_tokens": 1000},
            model="m", round_num=0, session_id="a",
        )
        await tracker.record(
            usage={"input_tokens": 50, "output_tokens": 50, "total_tokens": 100},
            model="m", round_num=0, session_id="b",
        )
        assert isinstance(tracker.session_total, int)  # 普通属性可读
        assert tracker.session_total == 100  # 最近记录会话 = b
        total = getattr(tracker, "session_total", 0)  # runtime_config 读取模式
        assert total == 100

    @pytest.mark.asyncio
    async def test_get_session_stats_zero_arg_uses_recent_session(self, tracker):
        """get_session_stats() 零参（ws_handler.py:98）与 get_session_stats(None) 返回最近记录会话。"""
        await tracker.record(
            usage={"input_tokens": 800, "output_tokens": 200, "total_tokens": 1000},
            model="m", round_num=0, session_id="a",
        )
        await tracker.record(
            usage={"input_tokens": 50, "output_tokens": 50, "total_tokens": 100},
            model="m", round_num=0, session_id="b",
        )
        stats_zero = await tracker.get_session_stats()
        stats_none = await tracker.get_session_stats(None)
        assert stats_zero["total"] == 100
        assert stats_none["total"] == 100
        assert stats_zero["by_model"] == {"m": 100}

    @pytest.mark.asyncio
    async def test_record_default_session_id_falls_back_to_self(self, tracker):
        """record 不传 session_id 时沿用实例 self.session_id。"""
        await tracker.record(
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            model="m", round_num=0,
        )
        assert tracker._session_totals[tracker.session_id] == 15
        stats = await tracker.get_session_stats()
        assert stats["total"] == 15
        assert stats["by_model"] == {"m": 15}

    @pytest.mark.asyncio
    async def test_get_session_stats_new_session_no_leak(self, tracker):
        """F8（Mongo 离线）：新会话无记录 → total=0/空，不泄漏最近会话累计。

        修复前内存回退 `_session_totals.get(sid, self.session_total)` 会返回
        最近记录会话的累计——新会话（v1 建连）显示上个会话 token 数。
        """
        await tracker.record(
            usage={"input_tokens": 800, "output_tokens": 200, "total_tokens": 1000},
            model="m", round_num=0, session_id="recent_sid",
        )
        assert tracker.session_total == 1000  # 最近记录会话语义保持（debug/runtime_config 兼容）
        stats = await tracker.get_session_stats("brand_new_sid")
        assert stats["total"] == 0      # 非上个会话累计
        assert stats["by_model"] == {}  # 非全局 per_model

    @pytest.mark.asyncio
    async def test_get_session_stats_mongodb_new_session_empty(self, output_mock):
        """F8（Mongo 在线）：新会话 DB 无记录 → 返回 0/空，不泄漏最近会话累计。"""
        repo_mock = MagicMock()
        repo_mock.total_tokens_by_model = AsyncMock(return_value={})
        repo_mock.record = AsyncMock()
        tracker = TokenTracker(
            session_id=SessionId("s1"), output_manager=output_mock, repo=repo_mock,
        )
        tracker._pending_file = "/tmp/test_stats_new_sid_nonexistent.jsonl"
        await tracker.record(
            usage={"input_tokens": 50, "output_tokens": 50, "total_tokens": 100},
            model="m", round_num=0, session_id="s1",
        )
        stats = await tracker.get_session_stats("new_sid")
        assert stats["total"] == 0
        assert stats["by_model"] == {}


@pytest.mark.unit
class TestTokenTrackerClearAll:
    @pytest.mark.asyncio
    async def test_clear_all_calls_repo_clear_all(self):
        repo_mock = MagicMock()
        repo_mock.clear_all = AsyncMock(return_value=42)
        deleted = await TokenTracker.clear_all(repo_mock)
        assert deleted == 42
        repo_mock.clear_all.assert_awaited_once()


@pytest.mark.unit
class TestPendingFileLifecycle:
    """`_pending_file` 的解析时机与隔离（2026-09-27 测试隔离专项）。

    ## 修的是什么

    旧实现是**类属性**：`_pending_file = str(ProjectPath.TOKEN_PENDING.resolve())`，
    在模块 import 时求值一次 ⇒ `ELYSIA_PATH_TOKEN_PENDING` 被永久固化。于是
    根 `conftest.py :: _isolate_shared_journals` 那套 env 覆盖（7 个 journal 都靠它
    隔离到 `tmp_path`）**对它无效**——测试可以写到仓库真实的
    `data/token_usage_pending.jsonl`。本类把新契约钉死。

    注意本类的用例**不**用 `monkeypatch` 去改实例属性：契约要的正是"按**构造时**
    的环境解析"，所以判据必须走 `monkeypatch.setenv` + 新建实例。
    """

    @pytest.fixture
    def output_mock(self):
        output = MagicMock()
        output.token_usage = AsyncMock()
        return output

    def _make(self, output_mock, **kwargs):
        return TokenTracker(
            session_id=SessionId("lifecycle"), output_manager=output_mock, **kwargs)

    def test_default_path_is_unchanged(self, output_mock, monkeypatch):
        """无 env 覆盖 → 仍是 `data/token_usage_pending.jsonl`（默认行为不变）。"""
        monkeypatch.delenv("ELYSIA_PATH_TOKEN_PENDING", raising=False)
        tracker = self._make(output_mock)
        assert tracker._pending_file.endswith("data/token_usage_pending.jsonl")

    def test_env_a_then_env_b_yield_different_paths(self, output_mock, monkeypatch):
        """**核心契约**：env A → 实例 A 用 A；改到 B → **新建**实例用 B。"""
        monkeypatch.setenv("ELYSIA_PATH_TOKEN_PENDING", "/tmp/lifecycle_a.jsonl")
        tracker_a = self._make(output_mock)
        assert tracker_a._pending_file == "/tmp/lifecycle_a.jsonl"

        monkeypatch.setenv("ELYSIA_PATH_TOKEN_PENDING", "/tmp/lifecycle_b.jsonl")
        tracker_b = self._make(output_mock)
        assert tracker_b._pending_file == "/tmp/lifecycle_b.jsonl"

        # 已构造的实例不被后续 env 改动影响（一个实例一个路径，flush 才安全）
        assert tracker_a._pending_file == "/tmp/lifecycle_a.jsonl"

    def test_already_imported_module_still_sees_new_env(self, output_mock, monkeypatch):
        """反证旧缺陷：模块**早已 import**（本文件顶部就 import 了）也不能固化。

        旧实现下这条必红——类属性在 import 时就定格，env 改了也不变。
        """
        monkeypatch.setenv("ELYSIA_PATH_TOKEN_PENDING", "/tmp/after_import.jsonl")
        assert self._make(output_mock)._pending_file == "/tmp/after_import.jsonl"

    def test_explicit_injection_wins_over_env(self, output_mock, monkeypatch):
        """显式注入（构造参数）优先于环境解析——测试不必再穿透私有属性。"""
        monkeypatch.setenv("ELYSIA_PATH_TOKEN_PENDING", "/tmp/from_env.jsonl")
        tracker = self._make(output_mock, pending_file="/tmp/injected.jsonl")
        assert tracker._pending_file == "/tmp/injected.jsonl"

    @pytest.mark.asyncio
    async def test_persist_writes_to_the_instance_path(self, output_mock, tmp_path,
                                                       monkeypatch):
        """行为判据：降级落盘真的写在这个实例的路径上（不只是属性好看）。"""
        target = tmp_path / "pending.jsonl"
        monkeypatch.setenv("ELYSIA_PATH_TOKEN_PENDING", str(target))
        tracker = self._make(output_mock)  # repo=None → 走本地 JSONL 降级
        await tracker.record(
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            model="m", round_num=0,
        )
        assert target.exists(), "落盘必须走实例解析出的路径"
        assert json.loads(target.read_text().strip())["total_tokens"] == 15

    def test_conftest_journal_isolation_reaches_the_tracker(self, output_mock):
        """跨层判据：根 conftest 的 journal 隔离必须**真的**作用到 TokenTracker。

        这是修复前不可能成立的一条：`_isolate_shared_journals` 用
        `ELYSIA_PATH_TOKEN_PENDING` 把它导到 `tmp_path`，而 import 期固化的类属性
        让该 env 形同虚设。判据写成"不落在仓库真实 `data/` 下"而不是比对具体路径，
        免得绑死 conftest 的实现细节。
        """
        tracker = self._make(output_mock)
        resolved = Path(tracker._pending_file).resolve()
        assert "token_pending.json" in resolved.name, (
            f"根 conftest 的隔离没生效，解析到 {resolved}")
        assert Path(_REPO_ROOT / "data") not in resolved.parents, (
            f"TokenTracker 仍指向仓库真实 data/：{resolved}")
