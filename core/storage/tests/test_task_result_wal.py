# 测试文件：core/storage/tests/test_task_result_wal.py
"""#6 硬杀窗口：后台任务终态本地 WAL（`core/storage/task_result_wal.py`）。

**被测的不变量**（每条都对应"硬杀后终态还在不在"的一个必要环节）：

1. **同步 fsync**——`record_terminal` 返回时记录必须已在盘上。若只 `write` 不
   `fsync`，掉电仍会丢（数据停在内核页缓存），"跨硬杀"就是空话。
2. **追加而非整体重写**——硬杀会截断最后一行（半行 JSON）。读取侧必须跳过损坏行，
   而不是整个文件解析失败（那等于把**所有**记录一起丢掉）。
3. **折叠语义**——终态"先写为准"（与引擎的终态唯一性守卫一致）、派发"后写覆盖"。
4. **只有派发记录 = 进程死在任务中途** → 候选为 interrupted。
5. **清理**——`forget` 只丢指定任务；清空即删文件（空 journal 不留空壳）。
6. **写失败不抛**——磁盘问题不能把任务终态推送弄挂；但必须计数 + 记 warning。
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from core.storage.task_result_wal import (
    KIND_DISPATCH,
    KIND_TERMINAL,
    STATUS_INTERRUPTED,
    TaskResultWAL,
    get_wal,
    reset_wal,
)

pytestmark = pytest.mark.unit


class TestSyncDurability:
    def test_record_terminal_fsyncs_before_returning(self, tmp_path, monkeypatch):
        """fsync 是"跨硬杀"的全部意义——只 write 不 fsync 的话掉电照样丢。"""
        calls: list[int] = []
        real_fsync = os.fsync
        monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        assert wal.record_terminal("wf-1", "completed", {"result": "结果"}) is True

        assert calls, "record_terminal 必须 fsync（否则记录只在内核页缓存里）"

    def test_dispatch_record_is_fsynced_too(self, tmp_path, monkeypatch):
        calls: list[int] = []
        real_fsync = os.fsync
        monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_dispatch("wf-1", "写报告", "2026-09-15T00:00:00Z")

        assert calls

    def test_record_readable_from_a_fresh_open(self, tmp_path):
        """落盘后立刻用新的文件句柄读回来（不依赖进程内缓存）。"""
        path = tmp_path / "wal.jsonl"
        wal = TaskResultWAL(str(path))
        wal.record_terminal("wf-1", "failed", {"error": "崩了"})

        with open(path, encoding="utf-8") as f:
            line = f.readline()

        assert json.loads(line)["data"] == {"error": "崩了"}


class TestAppendOnlyTolerance:
    def test_half_written_tail_is_skipped_not_fatal(self, tmp_path):
        """硬杀截断最后一行是**预期**情况，不是异常——前面完整的记录必须照常可读。"""
        path = tmp_path / "wal.jsonl"
        wal = TaskResultWAL(str(path))
        wal.record_terminal("wf-1", "completed", {"result": "完整"})
        wal.record_terminal("wf-2", "completed", {"result": "完整"})
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"kind": "termi')

        records = wal.read_raw()

        assert [r["task_id"] for r in records] == ["wf-1", "wf-2"]

    def test_missing_file_is_empty(self, tmp_path):
        assert TaskResultWAL(str(tmp_path / "nope.jsonl")).read_raw() == []

    def test_torn_multibyte_tail_does_not_lose_earlier_records(self, tmp_path):
        """硬杀截断在多字节字符中间——整体解码会让**整个文件**不可读（真丢数据）。

        这是本项目自己的用例发现并修掉的：最初 `read_raw` 以文本模式整体打开，
        中文意图/emoji 的半个字符就让全部记录一起消失；改成按字节读、逐行解码后，
        损伤被限制在受损的那一行。
        """
        path = tmp_path / "wal.jsonl"
        wal = TaskResultWAL(str(path))
        wal.record_terminal("wf-1", "completed", {"result": "完整的一条"})
        with open(path, "ab") as f:  # 半条 UTF-8 序列（"中" 的前两个字节）截断
            f.write(b'{"kind": "terminal", "task_id": "wf-cut", "data": {"result": "\xe4\xb8')

        records = wal.read_raw()

        assert [r["task_id"] for r in records] == ["wf-1"], \
            "受损行之后的记录可以丢，但受损行之前的必须全部保住"

    def test_non_dict_and_idless_lines_ignored(self, tmp_path):
        path = tmp_path / "wal.jsonl"
        path.write_text(
            '["not", "a", "dict"]\n'
            '{"kind": "terminal"}\n'
            '{"kind": "terminal", "task_id": "wf-1", "event": "completed"}\n',
            encoding="utf-8")

        assert [r["task_id"] for r in TaskResultWAL(str(path)).read_raw()] == ["wf-1"]


class TestReplayFolding:
    def test_terminal_wins_and_joins_dispatch_intent(self, tmp_path):
        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_dispatch("wf-1", "整理纪要", "2026-09-15T01:00:00Z")
        wal.record_terminal("wf-1", "completed", {"result": "好了"})

        cand = wal.replay()

        assert len(cand) == 1
        assert cand[0]["task_id"] == "wf-1"
        assert cand[0]["event"] == "completed"
        assert cand[0]["data"] == {"result": "好了"}
        assert cand[0]["original_intent"] == "整理纪要"
        assert cand[0]["dispatched_at"] == "2026-09-15T01:00:00Z"

    def test_first_terminal_wins(self, tmp_path):
        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_terminal("wf-1", "completed", {"result": "先到"})
        wal.record_terminal("wf-1", "failed", {"error": "后到"})

        cand = wal.replay()

        assert len(cand) == 1 and cand[0]["event"] == "completed"

    def test_last_dispatch_wins(self, tmp_path):
        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_dispatch("wf-1", "旧意图", "2026-09-15T00:00:00Z")
        wal.record_dispatch("wf-1", "新意图", "2026-09-15T05:00:00Z")

        cand = wal.replay()

        assert cand[0]["original_intent"] == "新意图"
        assert cand[0]["dispatched_at"] == "2026-09-15T05:00:00Z"

    def test_dispatch_only_is_interrupted(self, tmp_path):
        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_dispatch("wf-1", "跑全库扫描", "2026-09-15T02:00:00Z")

        cand = wal.replay()

        assert cand[0]["event"] is None
        assert cand[0]["status"] == STATUS_INTERRUPTED
        assert cand[0]["original_intent"] == "跑全库扫描"

    def test_order_follows_first_appearance(self, tmp_path):
        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_dispatch("wf-b", "B", "2026-09-15T00:00:00Z")
        wal.record_dispatch("wf-a", "A", "2026-09-15T00:00:01Z")

        assert [c["task_id"] for c in wal.replay()] == ["wf-b", "wf-a"]

    def test_empty_wal_replays_empty(self, tmp_path):
        assert TaskResultWAL(str(tmp_path / "wal.jsonl")).replay() == []


class TestForget:
    def test_forget_drops_only_listed_tasks(self, tmp_path):
        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_terminal("wf-1", "completed", {"result": "a"})
        wal.record_dispatch("wf-1", "A", "2026-09-15T00:00:00Z")
        wal.record_terminal("wf-2", "completed", {"result": "b"})

        wal.forget(["wf-1"])

        assert [r["task_id"] for r in wal.read_raw()] == ["wf-2"]

    def test_forget_all_deletes_file(self, tmp_path):
        path = tmp_path / "wal.jsonl"
        wal = TaskResultWAL(str(path))
        wal.record_terminal("wf-1", "completed", {"result": "a"})

        wal.forget(["wf-1"])

        assert not path.exists(), "空 journal 不应在磁盘上留一个空壳"

    def test_forget_unknown_id_is_noop(self, tmp_path):
        path = tmp_path / "wal.jsonl"
        wal = TaskResultWAL(str(path))
        wal.record_terminal("wf-1", "completed", {"result": "a"})
        before = path.read_text(encoding="utf-8")

        wal.forget(["wf-nope"])

        assert path.read_text(encoding="utf-8") == before

    def test_forget_empty_iterable_is_noop(self, tmp_path):
        path = tmp_path / "wal.jsonl"
        wal = TaskResultWAL(str(path))
        wal.record_terminal("wf-1", "completed", {"result": "a"})

        wal.forget([])

        assert path.exists()

    def test_clear_removes_everything(self, tmp_path):
        path = tmp_path / "wal.jsonl"
        wal = TaskResultWAL(str(path))
        wal.record_terminal("wf-1", "completed", {"result": "a"})
        wal.record_dispatch("wf-2", "B", "2026-09-15T00:00:00Z")

        wal.clear()

        assert wal.read_raw() == [] and not path.exists()


class TestWriteFailureSemantics:
    def test_write_failure_does_not_raise_but_counts(self, tmp_path, caplog):
        """磁盘问题不能把任务终态推送弄挂；但**不是静默**——计数 + warning。"""
        # 把 WAL 路径指到一个"父级是文件"的位置 → open 必然失败
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        wal = TaskResultWAL(str(blocker / "wal.jsonl"))

        with caplog.at_level(logging.WARNING):
            ok = wal.record_terminal("wf-1", "completed", {"result": "a"})

        assert ok is False
        assert wal.failure_count == 1
        assert [r for r in caplog.records if r.levelno >= logging.WARNING], \
            "失败必须留日志（#6 的保障降级不能让观测面看起来正常）"

    def test_failure_count_accumulates(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        wal = TaskResultWAL(str(blocker / "wal.jsonl"))

        wal.record_terminal("wf-1", "completed", {"result": "a"})
        wal.record_dispatch("wf-2", "B", "2026-09-15T00:00:00Z")

        assert wal.failure_count == 2

    def test_unreadable_file_is_tolerated_with_warning(self, tmp_path, caplog, monkeypatch):
        """读取侧的兜底：任何意外异常都必须降级成"空 + warning"，不得抛到调用方
        （启动路径上抛异常会拦住整个 Agent 启动）。"""
        path = tmp_path / "wal.jsonl"
        path.write_text('{"kind": "terminal", "task_id": "wf-1"}\n', encoding="utf-8")

        real_open = open

        def _boom(file, *a, **kw):  # noqa: ANN001, ANN002, ANN003
            if str(file) == str(path):
                raise OSError("disk vanished")
            return real_open(file, *a, **kw)

        monkeypatch.setattr("builtins.open", _boom)

        with caplog.at_level(logging.WARNING):
            assert TaskResultWAL(str(path)).read_raw() == []

        assert path.is_file(), "只是读失败，不得删/改文件"
        assert [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestSingleton:
    def test_get_wal_is_stable_and_reset_clears_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ELYSIA_PATH_TASK_RESULT_WAL", str(tmp_path / "a.jsonl"))
        reset_wal()
        try:
            first = get_wal()
            assert get_wal() is first

            monkeypatch.setenv("ELYSIA_PATH_TASK_RESULT_WAL", str(tmp_path / "b.jsonl"))
            assert get_wal() is first, "单例只在首次调用时解析路径（改环境变量不换实例）"

            reset_wal()
            assert get_wal().path.endswith("b.jsonl"), "reset 后按新路径重建"
        finally:
            reset_wal()

    def test_kind_constants(self):
        assert (KIND_DISPATCH, KIND_TERMINAL) == ("dispatch", "terminal")
