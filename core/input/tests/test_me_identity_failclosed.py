# core/input/tests/test_me_identity_failclosed.py
"""P0-3：/api/me 在权威身份不可用时 fail-closed（503，不伪造、不诱导重初始化）。

覆盖：
- 权威 store 未装载成功 → GET 503 STORAGE_UNAVAILABLE（而非 404「未初始化」）；
- 权威 store 未装载成功 → PUT 503 STORAGE_UNAVAILABLE（不写本地假象）；
- 本地模式（无 repo）仍保留 404「未初始化」语义；
- 权威装载成功后 GET 200 返回 Mongo 中的档案。
"""


import pytest
from aiohttp import web

from core.input.character_routes import register_character_routes
from core.profile.profile_store import UserIdentityStore
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.storage.user_identity_repo import UserIdentityRepository

pytestmark = pytest.mark.service


class _DownRepo(UserIdentityRepository):
    """Mongo 离线：connect 恒失败。"""

    async def connect(self) -> bool:
        return False


def _app(store: UserIdentityStore) -> web.Application:
    app = web.Application()
    app["user_identity_store"] = store
    register_character_routes(app)
    return app


@pytest.mark.asyncio
async def test_get_me_returns_503_when_authoritative_store_unavailable(aiohttp_client):
    store = UserIdentityStore(repo=_DownRepo())
    await store.connect(user_name="妖梦")
    assert store.get() is None

    client = await aiohttp_client(_app(store))
    resp = await client.get("/api/me")
    assert resp.status == 503
    body = await resp.json()
    assert body["code"] == "STORAGE_UNAVAILABLE"
    assert body["retryable"] is True


@pytest.mark.asyncio
async def test_put_me_returns_503_when_authoritative_store_unavailable(aiohttp_client):
    store = UserIdentityStore(repo=_DownRepo())
    await store.connect(user_name="妖梦")

    client = await aiohttp_client(_app(store))
    resp = await client.put("/api/me", json={
        "displayName": "妖梦", "expectedRevision": 1,
        "locale": "zh-CN", "timezone": "Asia/Shanghai",
    })
    assert resp.status == 503
    body = await resp.json()
    assert body["code"] == "STORAGE_UNAVAILABLE"
    assert body["retryable"] is True
    assert store.get() is None          # 未伪造本地档案


@pytest.mark.asyncio
async def test_get_me_keeps_404_semantics_in_local_mode(aiohttp_client):
    """无 repo 的本地模式：未 bootstrap → 仍是 404「未初始化」（语义不变）。"""
    store = UserIdentityStore()
    client = await aiohttp_client(_app(store))
    resp = await client.get("/api/me")
    assert resp.status == 404
    assert (await resp.json())["code"] == "USER_PROFILE_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_me_returns_authoritative_profile_after_load(aiohttp_client):
    db = FakeDocumentDBBackend()
    repo = UserIdentityRepository(db=db)
    await repo.connect()
    store = UserIdentityStore(repo=repo)
    loaded = await store.connect(user_name="妖梦")
    assert loaded is not None
    await store.update_if_revision(1, {"preferredAddress": "主人"})

    client = await aiohttp_client(_app(store))
    resp = await client.get("/api/me")
    assert resp.status == 200
    body = await resp.json()
    assert body["displayName"] == "妖梦"
    assert body["preferredAddress"] == "主人"
    assert body["revision"] == 2


@pytest.mark.asyncio
async def test_put_me_rejects_stale_revision(aiohttp_client):
    """CAS 冲突仍返回 409 + currentRevision（不因 fail-closed 改动而回退）。"""
    db = FakeDocumentDBBackend()
    repo = UserIdentityRepository(db=db)
    await repo.connect()
    store = UserIdentityStore(repo=repo)
    await store.connect(user_name="妖梦")
    await store.update_if_revision(1, {"displayName": "妖梦"})

    client = await aiohttp_client(_app(store))
    resp = await client.put("/api/me", json={
        "displayName": "新名", "expectedRevision": 1,
        "locale": "zh-CN", "timezone": "Asia/Shanghai",
    })
    assert resp.status == 409
    body = await resp.json()
    assert body["code"] == "USER_PROFILE_REVISION_CONFLICT"
    assert body["details"]["currentRevision"] == 2


@pytest.mark.asyncio
async def test_health_check_reflects_repo_probe():
    """恢复协调器探针语义：repo 不可达 → False（供 False→True 边界触发重载）。"""
    store = UserIdentityStore(repo=_DownRepo())
    assert store.is_authoritative is True
    assert await store.health_check() is False

    class _UpRepo(UserIdentityRepository):
        async def health_check(self) -> bool:
            return True

    assert await UserIdentityStore(repo=_UpRepo()).health_check() is True
    # 本地模式（无 repo）恒 True——不门禁
    assert await UserIdentityStore().health_check() is True
