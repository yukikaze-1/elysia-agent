# 测试文件：core/input/tests/test_commit_then_release.py
"""#41（CR-04 彻底版）：删除路径必须是「快照 → 提交 → 释放」，且失败方向正确。

**被测的不变式**（每一条都能用回退/注入证伪）：

1. **顺序**：主操作提交发生在附件释放**之前**。回退顺序（释放 → 提交）→ `_log` 变
   `["release", "commit"]` → 红。这是本文件的核心——报告 CR-04 的建议 #3
   （"把 release 移到 clear_all 提交之后"）如果只改返回值不改顺序，本用例必须抓住。
2. **失败方向**：提交失败 ⇒ 一次释放都不发生（"删除失败 → 无副作用"）。旧顺序下
   提交失败时附件已被软删（不可逆），本用例会红。
3. **提交后释放失败可见且可重试**：200 + `cleanup_pending` + journal 落盘；
   journal 在"重启"（新实例读同一路径）后仍在，重试成功后清空并删文件。
4. 释放幂等：journal 里已释放过的条目重复重试不会把 ref_count 减成负数
   （`remove_ref` 的条件 update 语义，由替身按真实条件实现来锁定）。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.schema.types import AttachmentId, SessionId
from core.storage import attachment_release as ar
from core.storage import pending_attachment_release as par
from core.storage.base import StorageUnavailableError

pytestmark = pytest.mark.service


def _msg(message_id: str, attachment_ids: list[str]) -> MagicMock:
    m = MagicMock()
    m.message_id = message_id
    m.content = [{"attachment_id": a} for a in attachment_ids]
    return m


def _agent_with_attachments(log: list[str] | None = None,
                            *, fail_remove: bool = False) -> MagicMock:
    """构造 agent：`attachment_repo.remove_ref` 按**真实条件语义**实现。

    真实 `remove_ref` 只在 `refs` 确实含该 message_id 时才 pull+dec（重复释放是
    no-op、不会减成负数）。替身必须忠实，否则"幂等"用例会假绿。
    """
    refs: dict[str, set[str]] = {}

    async def _remove_ref(aid, message_id):
        if fail_remove:
            raise StorageUnavailableError("mongo offline")
        if log is not None:
            log.append("release")
        refs.setdefault(str(aid), set()).discard(message_id)

    repo = MagicMock()
    repo.remove_ref = AsyncMock(side_effect=_remove_ref)
    repo.get = AsyncMock(return_value={"ref_count": 0})
    agent = MagicMock()
    agent.attachment_repo = repo
    return agent


@pytest.fixture(autouse=True)
def _isolate_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("ELYSIA_PATH_PENDING_ATTACHMENT_RELEASE",
                       str(tmp_path / "pending_release.json"))
    par.reset_journal()
    yield
    par.reset_journal()


# ══════════════════════════════════════════════════════════════════
# 1 + 2：顺序与失败方向（走真实 handler）
# ══════════════════════════════════════════════════════════════════

class TestOrderIsCommitThenRelease:
    @pytest.mark.asyncio
    async def test_archive_clear_commits_before_releasing(self):
        """顺序证据：调用日志必须是 commit → release（回退顺序即红）。"""
        from core.input.debug_routes import archive as mod

        log: list[str] = []

        async def _clear_all(*, confirm: str) -> int:
            log.append("commit")
            return 3

        agent = _agent_with_attachments(log)
        agent.message_repo = MagicMock()
        agent.message_repo.clear_all = AsyncMock(side_effect=_clear_all)
        agent.message_repo.query_offset = AsyncMock(return_value=([], 0))
        agent.session.messages = []

        request = MagicMock()
        request.app = {"agent": agent}
        request.query = {"confirm": "archive"}

        with mock.patch.object(mod, "collect_all_attachment_refs",
                              new=AsyncMock(return_value=[("m1", [AttachmentId("att_1")])])):
            resp = await mod.archive_clear_handler(request)

        assert resp.status == 200
        assert log == ["commit", "release"], (
            "必须是「先提交主操作、再释放附件引用」——"
            f"实得 {log}（release 先出现即 CR-04 的不可逆损失方向）")

    @pytest.mark.asyncio
    async def test_archive_clear_commit_failure_has_no_side_effect(self):
        """提交失败 ⇒ 一次释放都不发生（旧顺序下这里会已经软删全部附件）。"""
        from core.input.debug_routes import archive as mod

        log: list[str] = []
        agent = _agent_with_attachments(log)
        repo = MagicMock()
        repo.clear_all = AsyncMock(side_effect=StorageUnavailableError("offline"))
        repo.query_offset = AsyncMock(return_value=([], 0))
        agent.message_repo = repo
        agent.session.messages = []

        request = MagicMock()
        request.app = {"agent": agent}
        request.query = {"confirm": "archive"}

        with mock.patch.object(mod, "collect_all_attachment_refs",
                              new=AsyncMock(return_value=[("m1", [AttachmentId("att_1")])])):
            resp = await mod.archive_clear_handler(request)

        assert resp.status == 503
        assert log == [], "提交没成功 → 不允许有任何附件副作用"
        assert par.get_journal().pending_count() == 0, "没提交就谈不上待清理"

    @pytest.mark.asyncio
    async def test_session_delete_commits_before_releasing(self):
        from core.input.debug_routes import archive as mod

        log: list[str] = []

        async def _delete(sid) -> int:
            log.append("commit")
            return 1

        agent = _agent_with_attachments(log)
        agent.session_repo = MagicMock()
        agent.session_repo.health_check = AsyncMock(return_value=True)
        agent.session_repo.delete = AsyncMock(side_effect=_delete)
        agent.session.session_id = "other"

        request = MagicMock()
        request.app = {"agent": agent}
        request.match_info = {"session_id": "conv_x"}

        with mock.patch.object(mod, "collect_session_attachment_refs",
                              new=AsyncMock(return_value=[("m1", [AttachmentId("att_1")])])):
            resp = await mod.session_delete_handler(request)

        assert resp.status == 200
        assert log == ["commit", "release"], f"实得 {log}"


# ══════════════════════════════════════════════════════════════════
# 3 + 4：journal 落盘 / 跨"重启"存活 / 重试 / 幂等
# ══════════════════════════════════════════════════════════════════

class TestPendingReleaseJournal:
    @pytest.mark.asyncio
    async def test_release_failure_registers_and_returns_pending(self):
        agent = _agent_with_attachments(fail_remove=True)
        snap = [("m1", [AttachmentId("att_1")]), ("m2", [AttachmentId("att_2")])]

        pending = await ar.release_after_commit(agent, snap)

        assert pending is True
        journal = par.get_journal()
        assert journal.pending_count() == 2
        assert journal.snapshot() == snap
        assert Path(journal.path).is_file(), "必须落盘（否则重启即丢）"

    @pytest.mark.asyncio
    async def test_journal_survives_restart_and_retry_drains_it(self, tmp_path):
        """跨"重启"：新实例读同一路径仍能拿到待释放条目，重试成功后清空 + 删文件。"""
        path = str(tmp_path / "pending_release.json")
        j1 = par.PendingAttachmentRelease(path)
        j1.register([("m1", [AttachmentId("att_1")])])
        assert Path(path).is_file()

        # "重启"——新实例（模拟新进程读 journal）
        j2 = par.PendingAttachmentRelease(path)
        assert j2.pending_count() == 1

        agent = _agent_with_attachments()
        released = await j2.retry(agent)

        assert released == 1
        assert j2.pending_count() == 0
        assert not Path(path).exists(), "全部释放后必须删掉 journal 文件"
        agent.attachment_repo.remove_ref.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retry_keeps_entries_while_storage_down(self):
        path_agent = _agent_with_attachments(fail_remove=True)
        journal = par.get_journal()
        journal.register([("m1", [AttachmentId("att_1")])])

        released = await journal.retry(path_agent)

        assert released == 0
        assert journal.pending_count() == 1, "仍不可用 → 必须留到下次再试"

    @pytest.mark.asyncio
    async def test_release_is_idempotent_across_retries(self):
        """重复释放同一条目不得报错、不得重复减计数（替身按真实条件语义实现）。"""
        agent = _agent_with_attachments()
        snap = [("m1", [AttachmentId("att_1")])]

        assert await ar.release_refs_snapshot(agent, snap) == 1
        assert await ar.release_refs_snapshot(agent, snap) == 1

    def test_register_merges_duplicate_message_ids(self):
        journal = par.get_journal()
        journal.register([("m1", [AttachmentId("a1")])])
        journal.register([("m1", [AttachmentId("a1"), AttachmentId("a2")])])

        assert journal.snapshot() == [("m1", [AttachmentId("a1"), AttachmentId("a2")])]

    def test_corrupt_journal_is_ignored(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        journal = par.PendingAttachmentRelease(str(path))
        assert journal.pending_count() == 0

    @pytest.mark.asyncio
    async def test_handler_registers_journal_on_release_failure(self):
        """端到端：handler 的响应字段与磁盘 journal 必须一致（不是两条路径各写一份）。"""
        from core.input.debug_routes import archive as mod

        agent = _agent_with_attachments(fail_remove=True)
        agent.message_repo = MagicMock()
        agent.message_repo.clear_all = AsyncMock(return_value=2)
        agent.message_repo.query_offset = AsyncMock(return_value=([], 0))
        agent.session.messages = []

        request = MagicMock()
        request.app = {"agent": agent}
        request.query = {"confirm": "archive"}

        with mock.patch.object(mod, "collect_all_attachment_refs",
                              new=AsyncMock(return_value=[("m1", [AttachmentId("att_1")])])):
            resp = await mod.archive_clear_handler(request)

        assert resp.status == 200
        assert json.loads(resp.body.decode())["cleanup_pending"] is True
        assert par.get_journal().pending_count() == 1


# ══════════════════════════════════════════════════════════════════
# 快照采集：只收有附件的消息 + 分页遍历
# ══════════════════════════════════════════════════════════════════

class TestCollectAttachmentRefs:
    @pytest.mark.asyncio
    async def test_collects_only_messages_with_attachments(self):
        agent = MagicMock()
        agent.message_repo = MagicMock()
        agent.message_repo.query_offset = AsyncMock(return_value=(
            [_msg("m1", ["att_1"]), _msg("m2", []), _msg("m3", ["att_3", "att_4"])], 3))

        snap = await ar.collect_all_attachment_refs(agent)

        assert snap == [("m1", [AttachmentId("att_1")]),
                        ("m3", [AttachmentId("att_3"), AttachmentId("att_4")])]

    @pytest.mark.asyncio
    async def test_collect_paginates_until_short_page(self):
        """满页必须继续翻（否则第 201 条之后的附件引用永远拿不到 → 必然泄漏）。"""
        page = [_msg(f"m{i}", ["att"]) for i in range(ar._ATTACHMENT_RELEASE_BATCH)]
        agent = MagicMock()
        agent.message_repo = MagicMock()
        agent.message_repo.query_offset = AsyncMock(
            side_effect=[(page, len(page)), ([], 0)])

        snap = await ar.collect_all_attachment_refs(agent)

        assert len(snap) == ar._ATTACHMENT_RELEASE_BATCH
        assert agent.message_repo.query_offset.await_count == 2

    @pytest.mark.asyncio
    async def test_collect_session_refs_uses_id_cursor(self):
        agent = MagicMock()
        agent.message_repo = MagicMock()
        agent.message_repo.query_page = AsyncMock(return_value=([_msg("m1", ["att_1"])], False))

        snap = await ar.collect_session_attachment_refs(agent, SessionId("conv_1"))

        assert snap == [("m1", [AttachmentId("att_1")])]
