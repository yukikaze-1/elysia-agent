# core/profile/tests/test_profile_store.py
import pytest

from core.profile.profile_store import UserIdentityStore
from core.profile.user_identity import UserIdentity
from core.storage.base import StorageUnavailableError
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.storage.user_identity_repo import UserIdentityRepository

pytestmark = pytest.mark.service   # JSONL fallback，无 DB


def test_bootstrap_creates_from_user_name(tmp_path):
    store = UserIdentityStore(storage_path=str(tmp_path / "profile.json"))
    ui = store.bootstrap(user_name="妖梦")
    assert ui.displayName == "妖梦"
    assert ui.initialized is False
    assert ui.pronouns == "她"   # B1：全局唯一


def test_bootstrap_idempotent(tmp_path):
    store = UserIdentityStore(storage_path=str(tmp_path / "profile.json"))
    store.bootstrap(user_name="妖梦")
    ui2 = store.bootstrap(user_name="妖梦")
    assert ui2.displayName == "妖梦"
    assert ui2.revision == 1   # 二次 bootstrap 不重置


@pytest.mark.asyncio
async def test_save_touch_increments_revision(tmp_path):
    store = UserIdentityStore(storage_path=str(tmp_path / "profile.json"))
    ui = store.bootstrap(user_name="妖梦")
    saved = await store.save(ui)
    assert saved.revision == 2
    # 注：save() 原地 touch()（updatedAt + revision++）并返回同一对象；now_iso() 在
    # 同一微秒内可能返回相同时间戳（timing boundary），故不依赖 updatedAt 比较，
    # 改断言确定性递增的 revision（save 后 = 2，且 == 保存前 revision + 1）。
    assert saved.revision == ui.revision    # save 返回同一对象，revision 已递增到 2
    assert saved.revision == 2


@pytest.mark.asyncio
async def test_update_if_revision_success():
    store = UserIdentityStore(storage_path=None)
    store.bootstrap("妖梦")
    # bootstrap 后 revision=1
    result = await store.update_if_revision(1, {"displayName": "妖梦2"})
    assert result is not None and result.displayName == "妖梦2" and result.revision == 2


@pytest.mark.asyncio
async def test_update_if_revision_conflict():
    store = UserIdentityStore(storage_path=None)
    store.bootstrap("妖梦")
    result = await store.update_if_revision(99, {"displayName": "x"})  # 过期 revision
    assert result is None


@pytest.mark.asyncio
async def test_update_if_revision_unknown_field_skipped():
    """C3 修复：未知/非白名单字段跳过（而非 setattr 抛 ValueError → 500）。"""
    store = UserIdentityStore(storage_path=None)
    store.bootstrap("妖梦")
    result = await store.update_if_revision(1, {"displayName": "妖梦2", "bogus_field": 123})
    assert result is not None
    assert result.displayName == "妖梦2"   # 合法字段已应用
    assert result.revision == 2            # touch 正常
    assert not hasattr(result, "bogus_field")  # 未知字段未写入


@pytest.mark.asyncio
async def test_update_if_revision_readonly_fields_skipped():
    """userId/revision/createdAt/updatedAt 只读——更新时跳过。"""
    store = UserIdentityStore(storage_path=None)
    store.bootstrap("妖梦")
    result = await store.update_if_revision(1, {"userId": "usr_hacked", "revision": 999})
    assert result is not None
    assert result.userId == "usr_local_default"  # 未篡改
    assert result.revision == 2                   # 只 touch 递增，未被 999 覆盖


# ── BE-STAGEH-02：Mongo 权威路径 ──


def _repo(db: FakeDocumentDBBackend) -> UserIdentityRepository:
    return UserIdentityRepository(db=db)


@pytest.mark.asyncio
async def test_connect_loads_authoritative_profile_and_replaces_local_cache():
    db = FakeDocumentDBBackend()
    repo = _repo(db)
    await repo.connect()
    await repo.bootstrap_if_absent(
        UserIdentity(userId="usr_local_default", displayName="妖梦"))
    await repo.update_if_revision(
        "usr_local_default", 1, {"initialized": True, "preferredAddress": "主人"})

    store = UserIdentityStore(repo=repo)
    store.bootstrap(user_name="本地默认名")     # __init__ 的本地 bootstrap 先落位
    loaded = await store.connect(user_name="本地默认名")

    assert loaded.displayName == "妖梦"          # Mongo 覆盖本地
    assert loaded.preferredAddress == "主人"
    assert loaded.initialized is True
    assert loaded.revision == 2
    assert store.get() is loaded                 # 缓存已替换


@pytest.mark.asyncio
async def test_connect_bootstraps_when_mongo_empty_then_reloads():
    db = FakeDocumentDBBackend()
    repo = _repo(db)
    first = UserIdentityStore(repo=repo)
    created = await first.connect(user_name="妖梦")
    assert created.displayName == "妖梦" and created.revision == 1

    # 模拟重启：新 store 从同一 Mongo 读回，不重新 seed
    second = UserIdentityStore(repo=repo)
    reloaded = await second.connect(user_name="别人")
    assert reloaded.displayName == "妖梦"
    assert reloaded.revision == 1


@pytest.mark.asyncio
async def test_update_fails_closed_when_repo_configured_but_unavailable():
    """repo 已装配但未连接 → 503 语义，绝不悄悄写本地造成「已更新」假象。"""
    store = UserIdentityStore(repo=UserIdentityRepository(db=None))
    store.bootstrap("妖梦")
    with pytest.raises(StorageUnavailableError):
        await store.update_if_revision(1, {"displayName": "妖梦2"})


@pytest.mark.asyncio
async def test_connect_degrades_to_local_when_repo_unavailable():
    class _DownRepo(UserIdentityRepository):
        async def connect(self) -> bool:
            return False

    store = UserIdentityStore(repo=_DownRepo())
    loaded = await store.connect(user_name="妖梦")
    assert loaded.displayName == "妖梦"   # 降级本地，不抛
    assert store.get() is loaded
