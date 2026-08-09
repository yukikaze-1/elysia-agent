"""统一存储健康语义测试（B1/P1-05，marker=unit）。

覆盖：注入可达 fake → health_check True；ping_ok=False（模拟断网）→ health_check
False（`_db is not None` 不再代表健康）；恢复 ping_ok=True → health_check True
（fake 引用保留，不重连真实 Mongo）。
"""

from __future__ import annotations

import asyncio

import pytest

from core.storage.attachment_repo import AttachmentRepository
from core.storage.character_version_repo import CharacterVersionRepository
from core.storage.desktop_call_repo import DesktopCallRepository
from core.storage.generation_repo import GenerationRepository
from core.storage.message_repo import MessageRepository
from core.storage.session_repo import SessionRepository
from core.storage.token_repo import TokenRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.run(coro)


# 各 repo 构造器签名不同（db= 或 mongodb=）——统一注入 fake 的方式
_REPO_FACTORIES = {
    "session": lambda fake: SessionRepository(db=fake),
    "message": lambda fake: MessageRepository(db=fake),
    "token": lambda fake: TokenRepository(db=fake),
    "generation": lambda fake: GenerationRepository(db=fake),
    "attachment": lambda fake: AttachmentRepository(db=fake),
    "character_version": lambda fake: CharacterVersionRepository(db=fake),
    "desktop": lambda fake: DesktopCallRepository(mongodb=fake),
}


class TestPingBasedHealth:
    """`_db is not None` ≠ 健康：全部 repo 经真实 ping 判定。"""

    @pytest.mark.parametrize("name", list(_REPO_FACTORIES))
    def test_reachable_fake_health_true(self, name):
        fake = FakeDocumentDBBackend()          # ping_ok 默认 True
        repo = _REPO_FACTORIES[name](fake)
        assert _run(repo.health_check()) is True

    @pytest.mark.parametrize("name", list(_REPO_FACTORIES))
    def test_unreachable_fake_health_false(self, name):
        """断网（ping_ok=False）：health_check 必须 False——假阳性消除。"""
        fake = FakeDocumentDBBackend()
        fake.ping_ok = False
        repo = _REPO_FACTORIES[name](fake)
        assert _run(repo.health_check()) is False

    @pytest.mark.parametrize("name", list(_REPO_FACTORIES))
    def test_recovery_ping_ok_restores_health(self, name):
        """断网→恢复：fake 引用保留，ping_ok 转 True 后 health_check 恢复 True。"""
        fake = FakeDocumentDBBackend()
        fake.ping_ok = False
        repo = _REPO_FACTORIES[name](fake)
        assert _run(repo.health_check()) is False
        assert repo._db is fake          # 注入 fake 保留引用（不误建真实 Mongo）  # noqa: SLF001
        fake.ping_ok = True
        assert _run(repo.health_check()) is True


class TestUnconnectedSelfHeal:
    """未连接（db=None）时 health_check 尝试自愈重连。"""

    def test_unconnected_with_failing_backend_returns_false(self, monkeypatch):
        from Module.database.documentdb import DocumentDBFactory

        def _create_fail(*a, **kw):
            raise RuntimeError("Mongo unavailable")

        monkeypatch.setattr(DocumentDBFactory, "create", _create_fail)
        repo = MessageRepository(db=None)
        assert _run(repo.health_check()) is False
