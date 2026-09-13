# 测试文件：core/input/tests/test_debug_delete_release.py
"""S2-F2：debug 删除路径不得吞掉附件释放失败。

`core/storage/attachment_release.py` 的契约明确写：

> `StorageUnavailableError`: 附件存储不可用——**必须传播**（review 复核 #5），否则
> caller（会话删除 manager、debug 路由）无法感知释放失败，refs 残留导致附件被 GC
> 永久跳过或删除「假成功」。

而 debug 的两个删除路径用 `except Exception: logger.warning` 吞掉它，然后照常删除 →
**会话已删、refs 残留、响应称成功、无 journal 无重试**
（REST 侧 `SessionManager.delete` 有 cleanup + journal 通道）。

修法：保持**「未删除」语义**——释放失败即 503、什么都不删，客户端可安全重试。
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


class TestDebugDeleteDoesNotSwallowReleaseFailure:
    @pytest.mark.asyncio
    async def test_session_delete_refuses_when_release_fails(self):
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

        with mock.patch.object(mod, "release_session_attachments",
                               new=AsyncMock(side_effect=StorageUnavailableError("mongo"))):
            resp = await mod.session_delete_handler(request)

        assert resp.status == 503, f"释放失败必须 503，实得 {resp.status}"
        repo.delete.assert_not_awaited()          # **未删除**语义
        body = json.loads(resp.body.decode())
        assert body["code"] == "STORAGE_UNAVAILABLE"
        assert body["retryable"] is True

    @pytest.mark.asyncio
    async def test_sessions_clear_refuses_when_release_fails(self):
        from core.input.debug_routes import archive as mod
        from core.storage.base import StorageUnavailableError

        repo = MagicMock()
        repo.health_check = AsyncMock(return_value=True)
        repo.clear_all = AsyncMock(return_value=3)
        agent = MagicMock()
        agent.session_repo = repo
        agent.session.clear = MagicMock()

        request = _request(agent, {}, {"confirm": SESSION_CONFIRM_TOKEN})

        with mock.patch.object(mod, "release_all_attachments",
                               new=AsyncMock(side_effect=StorageUnavailableError("mongo"))):
            resp = await mod.sessions_clear_handler(request)

        assert resp.status == 503
        repo.clear_all.assert_not_awaited()       # 一条都不清
        assert json.loads(resp.body.decode())["code"] == "STORAGE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_archive_delete_refuses_when_release_fails(self):
        from core.input.debug_routes import archive as mod
        from core.storage.base import StorageUnavailableError

        msg = MagicMock()
        repo = MagicMock()
        repo.get_by_id = AsyncMock(return_value=msg)
        repo.delete_one = AsyncMock(return_value=True)
        agent = MagicMock()
        agent.message_repo = repo

        request = _request(agent, {"msg_id": "m1"})

        with mock.patch.object(mod, "release_message_attachments",
                               new=AsyncMock(side_effect=StorageUnavailableError("mongo"))):
            resp = await mod.archive_delete_handler(request)

        assert resp.status == 503, f"应给精确的 503（而非冒泡成 500），实得 {resp.status}"
        repo.delete_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_release_error_also_refuses(self):
        """非存储类异常同样不得"删了再说"——refs 残留的后果与错误类型无关。"""
        from core.input.debug_routes import archive as mod

        repo = MagicMock()
        repo.health_check = AsyncMock(return_value=True)
        repo.delete = AsyncMock(return_value=1)
        agent = MagicMock()
        agent.session_repo = repo

        request = _request(agent, {"session_id": "conv_f2b"})

        with mock.patch.object(mod, "release_session_attachments",
                               new=AsyncMock(side_effect=RuntimeError("boom"))):
            resp = await mod.session_delete_handler(request)

        assert resp.status == 503, f"释放失败不得继续删除，实得 {resp.status}"
        repo.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_release_still_deletes(self):
        """回归网：释放成功时行为不变。"""
        from core.input.debug_routes import archive as mod

        repo = MagicMock()
        repo.health_check = AsyncMock(return_value=True)
        repo.delete = AsyncMock(return_value=2)
        agent = MagicMock()
        agent.session_repo = repo
        agent.session.session_id = "other"

        request = _request(agent, {"session_id": "conv_ok"})

        with mock.patch.object(mod, "release_session_attachments", new=AsyncMock()):
            resp = await mod.session_delete_handler(request)

        assert resp.status == 200
        repo.delete.assert_awaited_once()


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
