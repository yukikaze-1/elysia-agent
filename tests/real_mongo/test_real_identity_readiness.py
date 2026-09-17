# 测试层：tests/real_mongo/test_real_identity_readiness.py
"""真实 MongoDB 层：N-1 / N-11 的「可达 ≠ 可用」在真库上成立吗（Batch 4）。

**为什么需要真库**：`test_ready_identity_truthfulness.py` 用 `FakeDocumentDBBackend`
构造"库通、文档坏"的状态——那是 fake 允许的任意文档。真库会不会也这样？会：
MongoDB 无 schema 校验，`revision: 0`（域模型约束 `ge=1`）这类文档写得进去、读不出来。
本文件用真驱动确认这条，从而证明 `/ready` 的 N-1 假绿不是 fake 的产物。

**只挑 fake 回答不了的问题**（对齐本层既有纪律）：真库真的能存下不合域模型的文档、
真 `ping()` 在那种状态下**仍然说通**、以及真实 `revision` CAS 之后的恢复。
"""

from __future__ import annotations

import pytest

from core.profile.profile_store import UserIdentityStore
from core.profile.user_identity import UserIdentity
from core.storage.user_identity_repo import COLLECTION, UserIdentityRepository

pytestmark = [
    pytest.mark.external,      # opt-in 层：默认任何档位都不跑（Normal = not external）
    pytest.mark.real_mongo,    # 本层专属 marker：CI 的 real-mongo job 用 -m real_mongo 选中
]


async def _real_store(real_mongo_repo) -> tuple[UserIdentityStore, UserIdentityRepository]:
    """真实仓储 + 真实 store（走 `connect()` 的真实装载路径）。"""
    repo = await real_mongo_repo(UserIdentityRepository)
    return UserIdentityStore(repo=repo), repo


@pytest.mark.asyncio
async def test_real_mongo_reachable_but_identity_unavailable(
        real_mongo_backend, real_mongo_repo):
    """真库上「ping 通、身份装不上」是可达状态 → 可用性必须为假且原因可查。

    直接往真集合写一条 `revision: 0` 的文档（Mongo 不校验域约束），然后走真实
    `UserIdentityStore.connect()`。
    """
    await real_mongo_backend.insert_one(COLLECTION, {
        "user_id": "usr_local_default",
        "display_name": "妖梦",
        "pronouns": "她",
        "locale": "zh-CN",
        "timezone": "Asia/Shanghai",
        "initialized": False,
        "default_character_id": None,
        "revision": 0,                      # 域模型 ge=1 → model_validate 抛错
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })

    store, repo = await _real_store(real_mongo_repo)
    loaded = await store.connect(user_name="妖梦")

    assert loaded is None, "不合域模型的文档不得被当成有效身份"
    assert store.get() is None
    assert store.is_available is False
    assert store.last_error == "ValidationError", (
        "真库路径的失败也必须留下可查状态（N-11），而不是只有一行日志")
    # 关键对照：**同一次真实 ping 说"库是通的"**——可达与可用不是一回事（N-1 根因）
    assert await repo.health_check() is True
    assert await store.health_check() is True


@pytest.mark.asyncio
async def test_real_recovery_restores_availability_and_readiness_input(real_mongo_backend,
                                                                      real_mongo_repo):
    """真库上的恢复：修好文档 → 真实 `connect()` 重载成功 → 可用性恢复、失败记录消解。

    这条对应恢复协调器在真库恢复后的动作（`_recover_dependency("identity")` →
    `store.connect()`），并证明 §21「失败历史不得钉死判定」在真驱动上成立。
    """
    store, repo = await _real_store(real_mongo_repo)
    first = await store.connect(user_name="妖梦")            # 空库 → 原子 bootstrap
    assert first is not None and store.is_available is True

    # 真库侧把文档改成不合域模型（模拟外部写入/版本回归造成的坏档案）
    db = real_mongo_backend
    await db.update_one(COLLECTION, {"user_id": "usr_local_default"},
                        {"$set": {"revision": 0}})

    assert await store.connect(user_name="妖梦") is None
    assert store.is_available is False
    assert store.last_error == "ValidationError"

    # 运维修好（真实 CAS 语义：revision 回到合法值且推进）
    await db.update_one(COLLECTION, {"user_id": "usr_local_default"},
                        {"$set": {"revision": 3, "display_name": "妖梦"}})

    restored = await store.connect(user_name="妖梦")

    assert restored is not None and restored.revision == 3
    assert store.is_available is True, "恢复后可用性必须回来（readiness 的输入随之回真）"
    assert store.last_error is None, "恢复后必须消解失败记录"
    assert await store.health_check() is True


@pytest.mark.asyncio
async def test_health_check_stays_repo_reachability_probe_on_real_driver(real_mongo_repo):
    """KEEP_AS_IS 的真库面：`health_check()` 是**可达性**探针，与可用性判据分离。

    最锋利的一步是 `close()` 之后：真驱动下 `health_check()` 会**自己重连**、把
    `repo.is_connected` 拉回真，于是 `is_available` 跟着变真——**但身份并没有被重新
    装载**。这既实证了"两个谓词回答两个问题"，也说明为什么 `/ready` 不能读
    `health_check()`：它会在「身份尚未就位」时凭一次重连变绿，而且带副作用。
    身份真正可用仍需 `connect()` 装载（进程启动就是这条路）。
    """
    store, _repo = await _real_store(real_mongo_repo)

    # 尚未任何装载：可用性为假，但库是通的
    assert store.is_available is False
    assert await store.health_check() is True

    await store.connect(user_name="妖梦")
    assert store.is_available is True
    assert await store.health_check() is True

    await store.close()
    assert store.is_available is False, "repo 连接状态已失效 → 可用性同步为假"

    # 探针自行重连（带副作用）→ 连接状态恢复，但**身份装载没有被它触发**
    assert await store.health_check() is True
    assert store.is_available is True, (
        "缓存仍在且 repo 已重连——这是可用性的既定口径（读路径服务缓存）")

    # 真正确认身份就位仍然只由 connect() 完成（恢复协调器走的也是它）
    reloaded = await store.connect(user_name="妖梦")
    assert reloaded is not None and store.is_available is True


@pytest.mark.asyncio
async def test_valid_profile_round_trips_through_store(real_mongo_repo):
    """正对照（ALREADY_GREEN_GUARD）：合法档案在真库上装载成功 → 可用。"""
    repo = await real_mongo_repo(UserIdentityRepository)
    await repo.bootstrap_if_absent(UserIdentity(userId="usr_local_default",
                                                displayName="妖梦"))
    await repo.update_if_revision("usr_local_default", 1,
                                  {"preferredAddress": "主人", "initialized": True})

    store = UserIdentityStore(repo=repo)
    loaded = await store.connect(user_name="启动默认名")

    assert loaded is not None
    assert loaded.displayName == "妖梦" and loaded.preferredAddress == "主人"
    assert store.is_available is True
    assert store.last_error is None
