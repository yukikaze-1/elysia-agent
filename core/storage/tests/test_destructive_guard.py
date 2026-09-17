"""破坏性 clear_all 的类型化确认守卫测试。

覆盖三层：
1. `require_confirm` 纯函数行为（含缺失/空串/错值）；
2. 三个仓储 `clear_all` 的守卫 + 存储不可用 fail-closed；
3. `LongTermMemory.clear_all` 透传确认、且**不再静默降级**。

背景见 `core/storage/destructive_guard.py`：此前 `DELETE /debug/api/{ltm,archive,sessions}`
无任何服务端守卫，且 `_db is None` 时静默返回——调用方会误以为已清空。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.storage.destructive_guard import (
    DestructiveConfirmRequired,
    require_confirm,
)
from core.storage.message_repo import (
    COLLECTION as MSG_COLLECTION,
)
from core.storage.message_repo import (
    CONFIRM_TOKEN as MSG_TOKEN,
)
from core.storage.message_repo import MessageRepository
from core.storage.session_repo import (
    CONFIRM_TOKEN as SESSION_TOKEN,
)
from core.storage.session_repo import SessionRepository
from core.storage.base import StorageUnavailableError


@pytest.mark.unit
class TestRequireConfirm:
    """`require_confirm` 纯函数：匹配放行，其余一律 fail-closed。"""

    def test_matching_token_passes(self):
        require_confirm(target="ltm", confirm="ltm", operation="清空长期记忆")

    @pytest.mark.parametrize("bad", [None, "", "LTM", "ltm ", "long_term_memory", "ltm2"])
    def test_mismatch_raises(self, bad):
        with pytest.raises(DestructiveConfirmRequired) as ei:
            require_confirm(target="ltm", confirm=bad, operation="清空长期记忆")
        # 错误信息必须自解释：告诉调用方该传什么
        assert "ltm" in str(ei.value)
        assert "清空长期记忆" in str(ei.value)

    def test_is_value_error_subclass(self):
        """ValueError 子类——API 误用不应被映射成 503。"""
        assert issubclass(DestructiveConfirmRequired, ValueError)


@pytest.mark.unit
class TestMessageRepositoryClearGuard:
    """MessageRepository.clear_all 的守卫。"""

    def test_confirm_token_is_stable_resource_name(self):
        """令牌是稳定资源名，不等于物理集合名（v1/v2 与测试重定向下均稳定）。"""
        assert MSG_TOKEN == "archive"
        assert MSG_TOKEN != MSG_COLLECTION

    def test_wrong_confirm_rejected_before_touching_db(self):
        repo = MessageRepository(db=MagicMock())
        repo._db.delete_many = AsyncMock(return_value=0)  # type: ignore[reportOptionalMemberAccess]
        with pytest.raises(DestructiveConfirmRequired):
            asyncio.run(repo.clear_all(confirm="archive_v2"))
        repo._db.delete_many.assert_not_awaited()  # type: ignore[reportOptionalMemberAccess]

    def test_missing_db_raises_instead_of_silent_zero(self):
        """此前 `_db is None` 静默返回 0 → 调用方误以为已清空。"""
        repo = MessageRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.clear_all(confirm=MSG_TOKEN))

    def test_correct_confirm_deletes(self):
        repo = MessageRepository(db=MagicMock())
        repo._db.delete_many = AsyncMock(return_value=7)  # type: ignore[reportOptionalMemberAccess]
        assert asyncio.run(repo.clear_all(confirm=MSG_TOKEN)) == 7


@pytest.mark.unit
class TestSessionRepositoryClearGuard:
    """SessionRepository.clear_all 的守卫。"""

    def test_confirm_token_is_stable_resource_name(self):
        assert SESSION_TOKEN == "sessions"

    def test_wrong_confirm_rejected(self):
        repo = SessionRepository(db=MagicMock())
        repo._db.delete_many = AsyncMock(return_value=0)  # type: ignore[reportOptionalMemberAccess]
        with pytest.raises(DestructiveConfirmRequired):
            asyncio.run(repo.clear_all(confirm="session"))
        repo._db.delete_many.assert_not_awaited()  # type: ignore[reportOptionalMemberAccess]

    def test_missing_db_raises_instead_of_silent_zero(self):
        repo = SessionRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.clear_all(confirm=SESSION_TOKEN))


@pytest.mark.unit
class TestMemoryRepositoryClearGuard:
    """MemoryRepository.clear_all 的守卫（drop + recreate，最危险）。"""

    def test_confirm_token_shared_by_v1_and_v2(self):
        """v1/v2 共用同一令牌——客户端无需猜服务端装配了哪个 repo。"""
        from core.storage.memory_repo import MemoryRepository
        from core.storage.memory_repo_v2 import MemoryRepositoryV2

        assert MemoryRepository.CONFIRM_TOKEN == MemoryRepositoryV2.CONFIRM_TOKEN == "ltm"

    def test_wrong_confirm_does_not_drop(self):
        repo = _memory_repo_with_mock_backend()
        with pytest.raises(DestructiveConfirmRequired):
            asyncio.run(repo.clear_all(confirm="long_term_memory_v2"))
        repo._db.drop_collection.assert_not_awaited()  # type: ignore[reportOptionalMemberAccess]

    def test_missing_db_raises_instead_of_silent_return(self):
        """此前 `_db is None` 直接 return（无返回值的静默成功）。"""
        from core.storage.memory_repo import MemoryRepository

        repo = MemoryRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.clear_all(confirm="ltm"))

    def test_correct_confirm_drops_and_recreates(self):
        repo = _memory_repo_with_mock_backend()
        asyncio.run(repo.clear_all(confirm="ltm"))
        repo._db.drop_collection.assert_awaited_once_with(repo.COLLECTION)  # type: ignore[reportOptionalMemberAccess]
        repo._db.create_collection.assert_awaited_once_with(repo.COLLECTION, repo.DIMENSION)  # type: ignore[reportOptionalMemberAccess]


def _memory_repo_with_mock_backend():
    """构造带 mock 后端的 MemoryRepository（绕过 connect）。"""
    from core.storage.memory_repo import MemoryRepository

    repo = MemoryRepository(db=MagicMock())
    repo._db.drop_collection = AsyncMock()  # type: ignore[reportOptionalMemberAccess]
    repo._db.create_collection = AsyncMock()  # type: ignore[reportOptionalMemberAccess]
    return repo
