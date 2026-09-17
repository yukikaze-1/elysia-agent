# core/input/tests/test_me_pronouns_roundtrip.py
"""`PUT /api/me` 的 pronouns 必须 write→store→read 对称（Batch 3.5 / B3-CB-1）。

## 缺陷背景

`UserIdentityProfile.pronouns` 的契约是 `Optional[str]` + `TsNull()`（wire 上
`string | null`，前端 `contract.generated.ts` 就是 `pronouns: string | null`），
`UserIdentityUpdate.pronouns` 也是 `Optional[str]`；设计稿 §5.1 把该字段列为
`string?`，§11.1 明确「省略 `preferredAddress`/`pronouns` 表示清空」，而
`character_routes._me_put` 正是把省略映射成 `None` 后落库。

但领域模型 `UserIdentity.pronouns` 曾是**非空** `str`，于是同一条链路上：

    write schema 允许 null（且产品语义要求 null = 已清空）
    → Mongo 原样存 null
    → _from_doc 用 UserIdentity.model_validate 读回 → ValidationError
    → PUT 自己就 500；重启后 connect() 装载失败 ⇒ store.get() 恒 None
    → /api/me 503、/ready 的 identity_ok 恒假 ⇒ 用户永久锁死

这一组用例把「写入即损坏」这条不变式钉在**真实 HTTP 入口**上（不是 mapper 单测）。

## 边界（刻意的）

- 只覆盖 pronouns 的 write/read 对称与其相邻语义（清空 vs 缺字段）。
- 不覆盖 `preferredAddress`/`defaultCharacterId`（同契约可空，但那是另一条 finding）。
"""

from __future__ import annotations

import pytest
from aiohttp import web

from core.input.character_routes import register_character_routes
from core.profile.profile_store import UserIdentityStore
from core.profile.user_identity import UserIdentity
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.storage.user_identity_repo import UserIdentityRepository

pytestmark = pytest.mark.service


def _app(store: UserIdentityStore) -> web.Application:
    app = web.Application()
    app["user_identity_store"] = store
    register_character_routes(app)
    return app


async def _authoritative_store(db: FakeDocumentDBBackend) -> UserIdentityStore:
    """权威模式（repo 已装配）的 store——生产装配路径。"""
    repo = UserIdentityRepository(db=db)
    await repo.connect()
    store = UserIdentityStore(repo=repo)
    await store.connect(user_name="妖梦")
    return store


def _put_body(**over: object) -> dict:
    """PUT /api/me 的完整表单基线（PUT 是完整替换，不是 PATCH）。"""
    body = {
        "displayName": "妖梦",
        "locale": "zh-CN",
        "timezone": "Asia/Shanghai",
        "expectedRevision": 1,
    }
    body.update(over)
    return body


# ── P1：合法 null PUT 后立即 GET（同一进程）──


@pytest.mark.asyncio
async def test_null_pronouns_put_then_get_roundtrips(aiohttp_client):
    """P1：`pronouns: null` 是合法写入；写完必须能立刻读回，且读回就是 null。"""
    db = FakeDocumentDBBackend()
    store = await _authoritative_store(db)
    client = await aiohttp_client(_app(store))

    put = await client.put("/api/me", json=_put_body(pronouns=None))
    assert put.status == 200, await put.text()
    assert (await put.json())["pronouns"] is None

    get = await client.get("/api/me")
    assert get.status == 200, await get.text()
    body = await get.json()
    assert body["pronouns"] is None
    assert body["displayName"] == "妖梦"
    assert body["revision"] == 2


# ── P2：非 null 正常 round-trip 不得被修复破坏 ──


@pytest.mark.asyncio
async def test_non_null_pronouns_roundtrips(aiohttp_client):
    """P2：非 null 值照常往返（修复不得把可空字段变成"总是 null"）。"""
    db = FakeDocumentDBBackend()
    store = await _authoritative_store(db)
    client = await aiohttp_client(_app(store))

    put = await client.put("/api/me", json=_put_body(pronouns="他"))
    assert put.status == 200, await put.text()

    get = await client.get("/api/me")
    assert get.status == 200, await get.text()
    assert (await get.json())["pronouns"] == "他"


@pytest.mark.asyncio
async def test_clearing_then_setting_pronouns_roundtrips(aiohttp_client):
    """清空 → 重新填值：两个方向都必须对称（防止只修单向）。"""
    db = FakeDocumentDBBackend()
    store = await _authoritative_store(db)
    client = await aiohttp_client(_app(store))

    await client.put("/api/me", json=_put_body(pronouns="他"))
    cleared = await client.put("/api/me", json=_put_body(pronouns=None, expectedRevision=2))
    assert cleared.status == 200, await cleared.text()
    assert (await cleared.json())["pronouns"] is None

    reset = await client.put("/api/me", json=_put_body(pronouns="她", expectedRevision=3))
    assert reset.status == 200, await reset.text()
    assert (await reset.json())["pronouns"] == "她"
    assert (await (await client.get("/api/me")).json())["pronouns"] == "她"


# ── P3：restart / reload 后仍可读（持久化面）──


@pytest.mark.asyncio
async def test_null_pronouns_survives_reload_from_authoritative_store(aiohttp_client):
    """P3：写入端落库的文档必须能被**新的** store 重新装载。

    这是 B3-CB-1 最致命的一面：损坏是持久的——重启后 `connect()` 装载失败，
    `get()` 恒 None（fail-closed 503），用户再也拿不回自己的档案。
    这里用同一个 fake Mongo 重新 `connect()` 来模拟重启（不靠进程内缓存）。
    """
    db = FakeDocumentDBBackend()
    store = await _authoritative_store(db)
    client = await aiohttp_client(_app(store))
    put = await client.put("/api/me", json=_put_body(pronouns=None))
    assert put.status == 200, await put.text()

    # 「重启」：新 repo + 新 store，只从已落库的文档装载
    reloaded = await _authoritative_store(db)
    assert reloaded.is_available is True
    profile = reloaded.get()
    assert profile is not None, "重启后权威档案装载失败——写入的 null 让记录永久不可读"
    assert profile.pronouns is None

    client2 = await aiohttp_client(_app(reloaded))
    resp = await client2.get("/api/me")
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["pronouns"] is None


# ── P4：真实数据库 round-trip（storage 层，同一 fake Mongo 后端）──


@pytest.mark.asyncio
async def test_repo_accepts_null_pronouns_and_reads_it_back():
    """P4：repository 层的 write→read 对称（不经 HTTP，直接压 repo 边界）。

    与 P1 的区别：P1 证明路由不 500，这一条证明**存储层本身**能把 null 读回，
    而不是靠路由层规避写入。
    """
    db = FakeDocumentDBBackend()
    repo = UserIdentityRepository(db=db)
    await repo.connect()
    await repo.bootstrap_if_absent(
        UserIdentity(userId="usr_local_default", displayName="妖梦"))

    updated = await repo.update_if_revision("usr_local_default", 1, {"pronouns": None})
    assert updated is not None
    assert updated.pronouns is None
    # 直读文档：null 确实落库（而不是被悄悄改写）
    assert db.collections["user_identity_profiles"][0]["pronouns"] is None
    # 再读一次（绕开返回值，走真正的 get 路径）
    assert (await repo.get("usr_local_default")).pronouns is None


# ── 邻域：null 与「字段缺失」不得混淆 ──


@pytest.mark.asyncio
async def test_missing_pronouns_key_in_legacy_doc_keeps_bootstrap_default(aiohttp_client):
    """旧记录（文档里没有 pronouns 键）必须仍读成 bootstrap 默认「她」。

    修复 nullable 字段时最容易引入的回归：把「字段缺失」也读成 null，会让所有
    存量记录（bootstrap 之前的形态）在 API 上突然变成「已清空」。缺字段 ≠ 显式 null。
    """
    db = FakeDocumentDBBackend()
    repo = UserIdentityRepository(db=db)
    await repo.connect()
    # 手工种一份**没有** pronouns 键的旧文档
    await db.insert_one("user_identity_profiles", {
        "user_id": "usr_local_default",
        "display_name": "妖梦",
        "locale": "zh-CN",
        "timezone": "Asia/Shanghai",
        "initialized": False,
        "revision": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    store = UserIdentityStore(repo=repo)
    loaded = await store.connect(user_name="妖梦")
    assert loaded is not None
    assert loaded.pronouns == "她"

    client = await aiohttp_client(_app(store))
    resp = await client.get("/api/me")
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["pronouns"] == "她"
