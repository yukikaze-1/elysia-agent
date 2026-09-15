# 测试文件：core/input/tests/test_debug_delete_release.py
"""S2-F2 + #41：debug 删除路径的附件释放语义。

`core/storage/attachment_release.py` 的契约明确写：

> `StorageUnavailableError`: 附件存储不可用——**必须传播**（review 复核 #5），否则
> caller（会话删除 manager、debug 路由）无法感知释放失败，refs 残留导致附件被 GC
> 永久跳过或删除「假成功」。

S2-F2 的修法是「释放失败 ⇒ 503 且什么都不删」（未删除语义）。**#41 之后这条修法
换了方向**：主操作（删会话 / 清空归档 / 删单条消息）改成**先提交再释放**，
于是——

- 提交**之前**失败（快照读取失败）→ 503 + 什么都没删（S2-F2 原语义保留）；
- 提交**之后**失败 → 200 + `cleanup_pending=true` + journal 登记（此时已无可回滚，
  再报"未删除（可重试）"是**谎报**：东西已经删了，而重试时快照已空、清理也不会
  再发生）。

两条合起来保证的是同一件事：**不存在"响应说没生效、实际却已发生不可逆副作用"的
状态**。断言因此分两半：旧的一半（读失败不删）在新文件前半段，新的一半
（提交后失败必须可见 + 可重试）在后半段。
"""

from __future__ import annotations

import json
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.storage.session_repo import CONFIRM_TOKEN as SESSION_CONFIRM_TOKEN

pytestmark = pytest.mark.service


def _request(agent, match_info=None, query=None):
    request = MagicMock()
    request.app = {"agent": agent}
    request.match_info = match_info or {}
    request.query = query or {}
    return request


@pytest.fixture(autouse=True)
def _isolate_journal(tmp_path, monkeypatch):
    """journal 落到 tmp_path（不碰真实 data/），并在用例后重置单例。"""
    from core.storage import pending_attachment_release as par

    monkeypatch.setenv("ELYSIA_PATH_PENDING_ATTACHMENT_RELEASE",
                       str(tmp_path / "pending_release.json"))
    par.reset_journal()
    yield
    par.reset_journal()


class TestDebugDeleteDoesNotSwallowReleaseFailure:
    """提交**前**读不到引用 → 拒绝删除（未删除语义，S2-F2 保留）。"""

    @pytest.mark.asyncio
    async def test_session_delete_refuses_when_snapshot_fails(self):
        from core.input.debug_routes import archive as mod
        from core.storage.base import StorageUnavailableError

        repo = MagicMock()
        repo.health_check = AsyncMock(return_value=True)
        repo.delete = AsyncMock(return_value=1)
        agent = MagicMock()
        agent.session_repo = repo
        agent.session.session_id = "other"
        agent.purge_message_from_session = MagicMock(return_value=False)

        request = _request(agent, {"session_id": "conv_f2"})

        with mock.patch.object(mod, "collect_session_attachment_refs",
                               new=AsyncMock(side_effect=StorageUnavailableError("mongo"))):
            resp = await mod.session_delete_handler(request)

        assert resp.status == 503, f"快照失败必须 503，实得 {resp.status}"
        repo.delete.assert_not_awaited()          # **未删除**语义
        body = json.loads(resp.body.decode())
        assert body["code"] == "STORAGE_UNAVAILABLE"
        assert body["retryable"] is True

    @pytest.mark.asyncio
    async def test_sessions_clear_refuses_when_snapshot_fails(self):
        from core.input.debug_routes import archive as mod
        from core.storage.base import StorageUnavailableError

        repo = MagicMock()
        repo.health_check = AsyncMock(return_value=True)
        repo.clear_all = AsyncMock(return_value=3)
        agent = MagicMock()
        agent.session_repo = repo
        agent.session.clear = MagicMock()

        request = _request(agent, {}, {"confirm": SESSION_CONFIRM_TOKEN})

        with mock.patch.object(mod, "collect_all_attachment_refs",
                               new=AsyncMock(side_effect=StorageUnavailableError("mongo"))):
            resp = await mod.sessions_clear_handler(request)

        assert resp.status == 503
        repo.clear_all.assert_not_awaited()       # 一条都不清
        assert json.loads(resp.body.decode())["code"] == "STORAGE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_archive_clear_refuses_when_snapshot_fails(self):
        from core.input.debug_routes import archive as mod
        from core.storage.base import StorageUnavailableError

        repo = MagicMock()
        repo.clear_all = AsyncMock(return_value=5)
        agent = MagicMock()
        agent.message_repo = repo
        agent.session.clear = MagicMock()

        request = _request(agent, {}, {"confirm": "archive"})

        with mock.patch.object(mod, "collect_all_attachment_refs",
                               new=AsyncMock(side_effect=StorageUnavailableError("mongo"))):
            resp = await mod.archive_clear_handler(request)

        assert resp.status == 503
        repo.clear_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_snapshot_error_also_refuses(self):
        """非存储类异常同样不得"删了再说"——refs 残留的后果与错误类型无关。"""
        from core.input.debug_routes import archive as mod

        repo = MagicMock()
        repo.health_check = AsyncMock(return_value=True)
        repo.delete = AsyncMock(return_value=1)
        agent = MagicMock()
        agent.session_repo = repo

        request = _request(agent, {"session_id": "conv_f2b"})

        with mock.patch.object(mod, "collect_session_attachment_refs",
                               new=AsyncMock(side_effect=RuntimeError("boom"))):
            resp = await mod.session_delete_handler(request)

        assert resp.status == 503, f"快照失败不得继续删除，实得 {resp.status}"
        repo.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_release_still_deletes(self):
        """回归网：一切正常时行为不变。"""
        from core.input.debug_routes import archive as mod

        repo = MagicMock()
        repo.health_check = AsyncMock(return_value=True)
        repo.delete = AsyncMock(return_value=2)
        agent = MagicMock()
        agent.session_repo = repo
        agent.session.session_id = "other"

        request = _request(agent, {"session_id": "conv_ok"})

        with mock.patch.object(mod, "collect_session_attachment_refs", new=AsyncMock(return_value=[])):
            resp = await mod.session_delete_handler(request)

        assert resp.status == 200
        repo.delete.assert_awaited_once()


class TestPostCommitReleaseFailureIsVisible:
    """提交**后**释放失败 → 200 + cleanup_pending + journal（#41 新增的一半）。

    这一半同样是"不得静默"：释放失败不再抛（抛了只能谎报"未删除"），但必须
    在响应里可见、并在磁盘上留下可重试的记录。
    """

    @pytest.mark.asyncio
    async def test_archive_clear_reports_cleanup_pending(self):
        from core.input.debug_routes import archive as mod

        repo = MagicMock()
        repo.clear_all = AsyncMock(return_value=5)
        agent = MagicMock()
        agent.message_repo = repo
        agent.session.messages = []

        request = _request(agent, {}, {"confirm": "archive"})

        with mock.patch.object(mod, "collect_all_attachment_refs",
                               new=AsyncMock(return_value=[("m1", ["att_1"])])), \
             mock.patch.object(mod, "release_after_commit", new=AsyncMock(return_value=True)):
            resp = await mod.archive_clear_handler(request)

        assert resp.status == 200, "主操作已提交，不得报错（那会是谎报）"
        body = json.loads(resp.body.decode())
        assert body["ok"] is True
        assert body["deleted"] == 5
        assert body["cleanup_pending"] is True

    @pytest.mark.asyncio
    async def test_session_delete_reports_cleanup_pending(self):
        from core.input.debug_routes import archive as mod

        repo = MagicMock()
        repo.health_check = AsyncMock(return_value=True)
        repo.delete = AsyncMock(return_value=1)
        agent = MagicMock()
        agent.session_repo = repo
        agent.session.session_id = "other"

        request = _request(agent, {"session_id": "conv_p"})

        with mock.patch.object(mod, "collect_session_attachment_refs",
                               new=AsyncMock(return_value=[("m1", ["att_1"])])), \
             mock.patch.object(mod, "release_after_commit", new=AsyncMock(return_value=True)):
            resp = await mod.session_delete_handler(request)

        assert resp.status == 200
        assert json.loads(resp.body.decode())["cleanup_pending"] is True
        repo.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_archive_delete_reports_cleanup_pending(self):
        from core.input.debug_routes import archive as mod

        msg = MagicMock()
        msg.content = [{"attachment_id": "att_9"}]
        repo = MagicMock()
        repo.get_by_id = AsyncMock(return_value=msg)
        repo.delete_one = AsyncMock(return_value=True)
        agent = MagicMock()
        agent.message_repo = repo
        agent.purge_message_from_session = MagicMock(return_value=0)

        request = _request(agent, {"msg_id": "m9"})

        with mock.patch.object(mod, "release_after_commit",
                               new=AsyncMock(return_value=True)):
            resp = await mod.archive_delete_handler(request)

        assert resp.status == 200
        assert json.loads(resp.body.decode())["cleanup_pending"] is True

    @pytest.mark.asyncio
    async def test_cleanup_pending_absent_on_success(self):
        """成功路径不得发射该字段（wire 与 #41 之前逐字节一致）。"""
        from core.input.debug_routes import archive as mod

        repo = MagicMock()
        repo.clear_all = AsyncMock(return_value=5)
        agent = MagicMock()
        agent.message_repo = repo
        agent.session.messages = []

        request = _request(agent, {}, {"confirm": "archive"})

        with mock.patch.object(mod, "collect_all_attachment_refs",
                               new=AsyncMock(return_value=[])):
            resp = await mod.archive_clear_handler(request)

        assert resp.status == 200
        assert "cleanup_pending" not in json.loads(resp.body.decode())


# ══════════════════════════════════════════════════════════════════
# S3-19：debug 归档路由的 ID 校验与写失败语义
# ══════════════════════════════════════════════════════════════════

class TestArchiveRouteIdValidation:
    """`ObjectId(msg_id)` 裸调用 → 畸形 ID 抛 `bson.InvalidId` 冒泡成 **500**。
    畸形 ID 是**客户端错误**，应当是 400 `CONTRACT_VALIDATION`
    （与 before/after 游标同一约定）。写失败也必须是 503 而非"成功 0 条"。"""

    @pytest.mark.asyncio
    async def test_delete_malformed_id_is_400(self):
        from core.input.debug_routes import archive as mod
        from core.storage.message_repo import InvalidMessageIdError

        repo = MagicMock()
        repo.get_by_id = AsyncMock(return_value=None)
        repo.delete_one = AsyncMock(
            side_effect=InvalidMessageIdError("非法消息 ID: 'abc'"))
        agent = MagicMock()
        agent.message_repo = repo

        resp = await mod.archive_delete_handler(_request(agent, {"msg_id": "abc"}))

        assert resp.status == 400, f"畸形 ID 应为 400（而非 500），实得 {resp.status}"
        body = json.loads(resp.body.decode())
        assert body["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_delete_storage_down_is_503(self):
        from core.input.debug_routes import archive as mod
        from core.storage.base import StorageUnavailableError

        repo = MagicMock()
        repo.get_by_id = AsyncMock(return_value=None)
        repo.delete_one = AsyncMock(side_effect=StorageUnavailableError("offline"))
        agent = MagicMock()
        agent.message_repo = repo

        resp = await mod.archive_delete_handler(_request(agent, {"msg_id": "a" * 24}))

        assert resp.status == 503
        assert json.loads(resp.body.decode())["retryable"] is True

    @pytest.mark.asyncio
    async def test_update_malformed_id_is_400(self):
        from core.input.debug_routes import archive as mod
        from core.storage.message_repo import InvalidMessageIdError

        repo = MagicMock()
        repo.update_text = AsyncMock(
            side_effect=InvalidMessageIdError("非法消息 ID: 'zz'"))
        agent = MagicMock()
        agent.message_repo = repo

        request = _request(agent, {"msg_id": "zz"})

        async def _json_object(_request):
            return {"text": "新文本"}

        with mock.patch.object(mod, "_json_object", new=_json_object):
            resp = await mod.archive_update_handler(request)

        assert resp.status == 400, f"实得 {resp.status}"
        assert json.loads(resp.body.decode())["code"] == "CONTRACT_VALIDATION"
