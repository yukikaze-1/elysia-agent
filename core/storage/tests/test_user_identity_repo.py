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

    outcome = await repo.update_if_revision(
        "usr_local_default", 1,
        {"displayName": "妖梦2", "pronouns": "他", "initialized": True})
    updated = outcome.identity
    assert updated is not None
    assert updated.displayName == "妖梦2"
    assert updated.pronouns == "他"
    assert updated.initialized is True
    assert updated.revision == 2
    assert outcome.current_revision is None      # 成功路径不携带冲突元数据


@pytest.mark.asyncio
async def test_update_if_revision_stale_reports_authoritative_revision():
    """N-3 / Batch 6：CAS 未命中必须携带**权威库现值**，而不是把真相压成 None。

    修前：返回裸 None ⇒ 上层只能回头问进程内缓存（多 writer 时是陈旧值）。
    """
    repo = UserIdentityRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    await repo.bootstrap_if_absent(_seed())

    outcome = await repo.update_if_revision(
        "usr_local_default", 99, {"displayName": "x"})

    assert outcome.applied is False
    assert outcome.identity is None
    assert outcome.current_revision == 1          # 权威就是这条文档的 revision


@pytest.mark.asyncio
async def test_update_if_revision_conflict_reads_authority_not_caller_state():
    """两个 repo 实例指向同一权威库：落败方报的是 winner 写后的现值（不是它自己读到的）。"""
    db_a = FakeDocumentDBBackend()
    db_b = FakeDocumentDBBackend()
    db_b.collections = db_a.collections          # 两条连接同一库
    loser_repo = UserIdentityRepository(db=db_a)
    winner_repo = UserIdentityRepository(db=db_b)
    await loser_repo.connect()
    await winner_repo.connect()
    await winner_repo.bootstrap_if_absent(_seed())

    assert (await winner_repo.update_if_revision(
        "usr_local_default", 1, {"displayName": "winner"})).identity is not None
    assert (await winner_repo.update_if_revision(
        "usr_local_default", 2, {"displayName": "winner2"})).identity is not None

    outcome = await loser_repo.update_if_revision(
        "usr_local_default", 1, {"displayName": "loser"})

    assert outcome.applied is False
    assert outcome.current_revision == 3          # 权威库现值，而非落败方手里的 1
    assert db_a.collections[COLLECTION][0]["display_name"] == "winner2"


@pytest.mark.asyncio
async def test_update_if_revision_conflict_without_record_reports_unknown():
    """记录不存在 ⇒ 现值**未知**（None），不得伪造成某个整数（§31/§33 fail-closed）。"""
    repo = UserIdentityRepository(db=FakeDocumentDBBackend())
    await repo.connect()

    outcome = await repo.update_if_revision(
        "usr_local_default", 1, {"displayName": "x"})

    assert outcome.applied is False
    assert outcome.current_revision is None


@pytest.mark.asyncio
async def test_update_if_revision_clears_nullable_field():
    """PUT /api/me 的冻结语义：省略 nullable 字段即清空。"""
    repo = UserIdentityRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    await repo.bootstrap_if_absent(_seed())
    await repo.update_if_revision(
        "usr_local_default", 1, {"preferredAddress": "主人"})
    cleared = (await repo.update_if_revision(
        "usr_local_default", 2, {"preferredAddress": None})).identity
    assert cleared is not None and cleared.preferredAddress is None


@pytest.mark.asyncio
async def test_update_if_revision_skips_unknown_and_readonly_fields():
    repo = UserIdentityRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    await repo.bootstrap_if_absent(_seed())

    updated = (await repo.update_if_revision(
        "usr_local_default", 1,
        {"userId": "usr_hacked", "revision": 999, "createdAt": "1970-01-01T00:00:00Z",
         "bogus": 1, "displayName": "妖梦2"})).identity
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
