# core/profile/tests/test_profile_store.py
import pytest
from pathlib import Path
from core.profile.profile_store import UserIdentityStore

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


def test_save_touch_increments_revision(tmp_path):
    store = UserIdentityStore(storage_path=str(tmp_path / "profile.json"))
    ui = store.bootstrap(user_name="妖梦")
    saved = store.save(ui)
    assert saved.revision == 2
    # 注：save() 原地 touch()（updatedAt + revision++）并返回同一对象；now_iso() 在
    # 同一微秒内可能返回相同时间戳（timing boundary），故不依赖 updatedAt 比较，
    # 改断言确定性递增的 revision（save 后 = 2，且 == 保存前 revision + 1）。
    assert saved.revision == ui.revision    # save 返回同一对象，revision 已递增到 2
    assert saved.revision == 2


def test_update_if_revision_success():
    store = UserIdentityStore(storage_path=None)
    store.bootstrap("妖梦")
    # bootstrap 后 revision=1
    result = store.update_if_revision(1, {"displayName": "妖梦2"})
    assert result is not None and result.displayName == "妖梦2" and result.revision == 2


def test_update_if_revision_conflict():
    store = UserIdentityStore(storage_path=None)
    store.bootstrap("妖梦")
    result = store.update_if_revision(99, {"displayName": "x"})  # 过期 revision
    assert result is None
