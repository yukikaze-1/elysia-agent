"""UserIdentityRepository 测试（marker=service，内存假后端）。

BE-STAGEH-02：用户身份是 Mongo 权威事实源——本文件锁定唯一 bootstrap、revision CAS
与 fail-closed 语义。
"""
import pytest

from core.profile.user_identity import UserIdentity
from core.storage.base import StorageUnavailableError
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.storage.user_identity_repo import (
    COLLECTION,
    UserIdentityRepository,
)

# 文件整体性质一致：单模块 repo 测试 + 内存假后端（无真实 DB）。
pytestmark = pytest.mark.service


def _seed(user_name: str = "妖梦") -> UserIdentity:
    return UserIdentity(userId="usr_local_default", displayName=user_name)


@pytest.mark.asyncio
async def test_get_absent_returns_none():
    repo = UserIdentityRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    assert await repo.get("usr_local_default") is None


@pytest.mark.asyncio
async def test_bootstrap_if_absent_is_idempotent_and_never_overwrites():
    db = FakeDocumentDBBackend()
    repo = UserIdentityRepository(db=db)
    await repo.connect()

    first = await repo.bootstrap_if_absent(_seed("妖梦"))
    assert first.displayName == "妖梦"
    assert first.revision == 1
    assert first.initialized is False

    # 第二次（模拟并发启动的失败方）不得覆盖已有文档
    second = await repo.bootstrap_if_absent(_seed("另一个人"))
    assert second.displayName == "妖梦"
    assert second.revision == 1
    assert len(db.collections[COLLECTION]) == 1


@pytest.mark.asyncio
async def test_update_if_revision_applies_and_bumps_revision():
    repo = UserIdentityRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    await repo.bootstrap_if_absent(_seed())

    updated = await repo.update_if_revision(
        "usr_local_default", 1,
        {"displayName": "妖梦2", "pronouns": "他", "initialized": True})
    assert updated is not None
    assert updated.displayName == "妖梦2"
    assert updated.pronouns == "他"
    assert updated.initialized is True
    assert updated.revision == 2


@pytest.mark.asyncio
async def test_update_if_revision_stale_returns_none():
    repo = UserIdentityRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    await repo.bootstrap_if_absent(_seed())
    assert await repo.update_if_revision(
        "usr_local_default", 99, {"displayName": "x"}) is None


@pytest.mark.asyncio
async def test_update_if_revision_clears_nullable_field():
    """PUT /api/me 的冻结语义：省略 nullable 字段即清空。"""
    repo = UserIdentityRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    await repo.bootstrap_if_absent(_seed())
    await repo.update_if_revision(
        "usr_local_default", 1, {"preferredAddress": "主人"})
    cleared = await repo.update_if_revision(
        "usr_local_default", 2, {"preferredAddress": None})
    assert cleared is not None and cleared.preferredAddress is None


@pytest.mark.asyncio
async def test_update_if_revision_skips_unknown_and_readonly_fields():
    repo = UserIdentityRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    await repo.bootstrap_if_absent(_seed())

    updated = await repo.update_if_revision(
        "usr_local_default", 1,
        {"userId": "usr_hacked", "revision": 999, "createdAt": "1970-01-01T00:00:00Z",
         "bogus": 1, "displayName": "妖梦2"})
    assert updated is not None
    assert updated.userId == "usr_local_default"
    assert updated.displayName == "妖梦2"
    assert updated.revision == 2  # 只按 CAS 递增，未被 999 覆盖


@pytest.mark.asyncio
async def test_operations_fail_closed_without_connection():
    repo = UserIdentityRepository(db=None)
    with pytest.raises(StorageUnavailableError):
        await repo.get("usr_local_default")
    with pytest.raises(StorageUnavailableError):
        await repo.bootstrap_if_absent(_seed())
    with pytest.raises(StorageUnavailableError):
        await repo.update_if_revision("usr_local_default", 1, {"displayName": "x"})


@pytest.mark.asyncio
async def test_health_check_follows_ping():
    db = FakeDocumentDBBackend()
    repo = UserIdentityRepository(db=db)
    await repo.connect()
    assert await repo.health_check() is True
    db.ping_ok = False
    assert await repo.health_check() is False
