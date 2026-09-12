# 测试文件：core/input/tests/test_destructive_confirm_ordering.py
"""CR-04 回归守卫：类型化确认必须**先于任何副作用**。

背景（评测报告 CR-04 / 根因簇 RC-4、RC-9）：`archive_clear_handler` 与
`sessions_clear_handler` 此前把 `release_all_attachments()`（**不可逆**的附件软删）
排在 `clear_all(confirm=...)` 的 confirm 校验**之前**，于是 `?confirm=<写错>` 的请求
先软删全部附件、再返回 400 —— 即「防误删守卫触发了不可逆删除」。

本文件断言的是**顺序**，不是返回值：仅在 confirm 合法时副作用才允许发生。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from core.agent import RunState
from core.input.server import create_app
from core.storage.destructive_guard import require_confirm
from core.storage.message_repo import CONFIRM_TOKEN as MESSAGE_CONFIRM_TOKEN
from core.storage.session_repo import CONFIRM_TOKEN as SESSION_CONFIRM_TOKEN


def _faithful_clear_all(token: str, deleted: int) -> AsyncMock:
    """模拟真实仓储的 `clear_all`：confirm 不匹配即抛 `DestructiveConfirmRequired`。

    必须**忠实**——若替身无条件返回成功，旧代码在 `confirm=typo` 下会返回 200，
    测试就退化成"断言状态码"而看不到真正的缺陷（副作用已发生）。
    用 `side_effect` 包住，保留 `call_count` 以便断言"未被调用"。
    """

    async def _impl(*, confirm: str) -> int:
        require_confirm(target=token, confirm=confirm, operation="测试清空")
        return deleted

    return AsyncMock(side_effect=_impl)


def _make_agent(confirm_ok: bool = True) -> MagicMock:
    agent = MagicMock()
    agent.output = MagicMock()
    agent.bus = MagicMock()
    agent._state = RunState.RUNNING
    agent.user_name = "tester"
    agent.session = MagicMock()
    agent.session.messages = []

    async def _health_ok():
        return True

    agent.session_repo = MagicMock()
    agent.session_repo.health_check = _health_ok
    agent.session_repo.clear_all = _faithful_clear_all(SESSION_CONFIRM_TOKEN, 3)
    agent.message_repo = MagicMock()
    agent.message_repo.clear_all = _faithful_clear_all(MESSAGE_CONFIRM_TOKEN, 7)
    return agent


@pytest.fixture
def spy_release(monkeypatch):
    """替换 debug_routes.archive 命名空间里的 release_all_attachments 为间谍。"""
    import core.input.debug_routes.archive as archive_mod

    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(archive_mod, "release_all_attachments", spy)
    return spy


async def _client(aiohttp_client):
    app = create_app(_make_agent(), serve_static=False)
    return await aiohttp_client(app)


@pytest.mark.service
class TestArchiveClearConfirmOrdering:
    """`DELETE /debug/api/archive` 的确认先于附件释放。"""

    @pytest.mark.asyncio
    async def test_wrong_confirm_400_and_no_side_effect(self, aiohttp_client, spy_release):
        """confirm 写错 → 400，且**一次附件释放都不得发生**（原缺陷的核心断言）。"""
        client = await _client(aiohttp_client)
        resp = await client.delete("/debug/api/archive?confirm=typo")
        assert resp.status == 400
        assert spy_release.call_count == 0, "confirm 校验必须先于副作用"
        assert client.app["agent"].message_repo.clear_all.call_count == 0

    @pytest.mark.asyncio
    async def test_missing_confirm_400_and_no_side_effect(self, aiohttp_client, spy_release):
        client = await _client(aiohttp_client)
        resp = await client.delete("/debug/api/archive")
        assert resp.status == 400
        assert spy_release.call_count == 0

    @pytest.mark.asyncio
    async def test_empty_confirm_400_and_no_side_effect(self, aiohttp_client, spy_release):
        client = await _client(aiohttp_client)
        resp = await client.delete("/debug/api/archive?confirm=")
        assert resp.status == 400
        assert spy_release.call_count == 0

    @pytest.mark.asyncio
    async def test_correct_confirm_still_works(self, aiohttp_client, spy_release):
        """合法 confirm → 副作用发生且返回 200（收口不得误伤正常路径）。"""
        client = await _client(aiohttp_client)
        resp = await client.delete("/debug/api/archive?confirm=archive")
        assert resp.status == 200
        assert spy_release.call_count == 1
        body = await resp.json()
        assert body["deleted"] == 7


@pytest.mark.service
class TestSessionsClearConfirmOrdering:
    """`DELETE /debug/api/sessions` 的确认先于附件释放。"""

    @pytest.mark.asyncio
    async def test_wrong_confirm_400_and_no_side_effect(self, aiohttp_client, spy_release):
        client = await _client(aiohttp_client)
        resp = await client.delete("/debug/api/sessions?confirm=typo")
        assert resp.status == 400
        assert spy_release.call_count == 0, "confirm 校验必须先于副作用"
        assert client.app["agent"].session_repo.clear_all.call_count == 0

    @pytest.mark.asyncio
    async def test_missing_confirm_400_and_no_side_effect(self, aiohttp_client, spy_release):
        client = await _client(aiohttp_client)
        resp = await client.delete("/debug/api/sessions")
        assert resp.status == 400
        assert spy_release.call_count == 0

    @pytest.mark.asyncio
    async def test_correct_confirm_still_works(self, aiohttp_client, spy_release):
        client = await _client(aiohttp_client)
        resp = await client.delete("/debug/api/sessions?confirm=sessions")
        assert resp.status == 200
        assert spy_release.call_count == 1
        body = await resp.json()
        assert body["deleted"] == 3
