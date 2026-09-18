# core/input/tests/test_me_conflict_metadata_authority.py
"""N-3 / Batch 6：409 `details.currentRevision` 必须是**权威库现值**（Conflict Metadata Truthfulness）。

## 为什么需要这个文件

`PUT /api/me` 的 CAS 冲突响应此前从 `UserIdentityStore.get()`（**进程内缓存**）取
`currentRevision`。同一 store 单实例自导自演的测试永远看不到问题：winner 若就是这个
store，缓存早已被 winner 刷新成新值。要暴露谎言必须有**两个独立 writer**：
Store A → Repo A、Store B → Repo B，共享同一份权威数据。

## 本文件的证据形态

- **两个独立 store/repo 实例**（不是 `store._cached.revision = …` 手工改字段）——见 §25；
- 每个 fake 后端是独立对象、只共享底层集合，等于两条连接打同一个库（§24：fake 与真库
  走**同一段** repo 冲突代码，权威回读发生在 repo 内，fake 不比 Mongo 强）；
- 走**真实 HTTP 路由** `PUT /api/me`（不是直接调 formatter，也不是只调 repository helper）；
- **不使用本批新增的返回类型**：winner 写入只看权威库里的事实（revision / 文档内容），
  于是这份用例在修前（旧返回类型）与修后（新结果对象）跑的是同一段判据——
  红色来自 metadata 值本身，而不是 API 形状变化。

修前（N-3）：`409.details.currentRevision` = 1（Store A 的陈旧缓存），权威库 = 2/3 ⇒ **红**。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from aiohttp import web

from core.input.character_routes import register_character_routes
from core.profile.profile_store import UserIdentityStore
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.storage.user_identity_repo import COLLECTION, UserIdentityRepository

pytestmark = pytest.mark.service

_UPDATE = {
    "displayName": "A 的 payload",
    "locale": "zh-CN",
    "timezone": "Asia/Shanghai",
}


class _ReadFailAfterSeedDB(FakeDocumentDBBackend):
    """种子完成后让 `find` 失败：模拟「CAS 冲突 → 权威回读失败」（§32）。"""

    fail_find: bool = False

    async def find(self, collection: str, filter_expr: dict, **kwargs: Any) -> list[dict]:
        if self.fail_find:
            raise RuntimeError("权威回读失败（替身）")
        return await super().find(collection, filter_expr, **kwargs)


class _SharedMongo:
    """两个后端对象共享同一份集合 = 两个独立 writer 打同一个权威库。"""

    def __init__(self, *, fail_a_read: bool = False) -> None:
        db_cls = _ReadFailAfterSeedDB if fail_a_read else FakeDocumentDBBackend
        self.db_a: FakeDocumentDBBackend = db_cls()
        self.db_b = FakeDocumentDBBackend()
        # 独立对象、共享数据（对齐「两条连接同一个 Mongo」，而非共享同一个后端句柄）
        self.db_b.collections = self.db_a.collections
        self.db_b.indexes = self.db_a.indexes

    async def stores(self) -> tuple[UserIdentityStore, UserIdentityStore]:
        repo_a = UserIdentityRepository(db=self.db_a)
        repo_b = UserIdentityRepository(db=self.db_b)
        await repo_a.connect()
        await repo_b.connect()
        store_a = UserIdentityStore(repo=repo_a)
        store_b = UserIdentityStore(repo=repo_b)
        assert await store_a.connect(user_name="妖梦") is not None
        assert await store_b.connect(user_name="妖梦") is not None
        return store_a, store_b

    @property
    def authoritative_revision(self) -> int:
        """权威库里那条文档的 revision（测试直接读库，不经任何缓存）。"""
        return self.db_a.collections[COLLECTION][0]["revision"]

    @property
    def authoritative_doc(self) -> dict:
        return self.db_a.collections[COLLECTION][0]


def _app(store: UserIdentityStore) -> web.Application:
    app = web.Application()
    app["user_identity_store"] = store
    register_character_routes(app)
    return app


async def _put(client: Any, expected_revision: int,
               display_name: str = "A 的 payload") -> tuple[int, dict]:
    body = dict(_UPDATE, displayName=display_name, expectedRevision=expected_revision)
    resp = await client.put("/api/me", json=body)
    return resp.status, await resp.json()


async def _two_writer_setup(
        *, fail_a_read: bool = False) -> tuple[_SharedMongo, UserIdentityStore,
                                               UserIdentityStore]:
    shared = _SharedMongo(fail_a_read=fail_a_read)
    store_a, store_b = await shared.stores()
    return shared, store_a, store_b


# ── Test CMT-1：两个 writer，冲突必须报 winner 的 revision ──


@pytest.mark.asyncio
async def test_cmt1_conflict_reports_authoritative_revision(aiohttp_client):
    """A/B 都读 R1；B 写入成功 → Mongo R2；A 携带 expected=1 → 409 currentRevision=2。

    修前：A 的缓存仍是 R1 ⇒ `details.currentRevision = 1`（谎报）。
    """
    shared, store_a, store_b = await _two_writer_setup()
    assert store_a.get().revision == 1 and store_b.get().revision == 1
    assert shared.authoritative_revision == 1

    await store_b.update_if_revision(1, {"displayName": "B 赢了"})
    assert shared.authoritative_revision == 2

    client = await aiohttp_client(_app(store_a))
    status, body = await _put(client, expected_revision=1)

    assert status == 409
    assert body["code"] == "USER_PROFILE_REVISION_CONFLICT"
    assert body["details"]["currentRevision"] == shared.authoritative_revision == 2, (
        f"409 必须报权威库现值（2），实际 {body['details'].get('currentRevision')!r}"
        f"（request=1 / A 缓存={store_a.get().revision} / Mongo={shared.authoritative_revision}）")


# ── Test CMT-2：缓存陈旧时不得冒充权威（本批最关键的一条）──


@pytest.mark.asyncio
async def test_cmt2_stale_cache_never_reported_as_current(aiohttp_client):
    """A 的 store 缓存停在 R1、权威库已是 R2 ⇒ 409 必须是 R2，不是缓存的 R1。

    四步显式取证：request revision / A 缓存 revision / Mongo authoritative revision /
    409 currentRevision —— 全部断言出来，避免"恰好相等"造成的假绿。
    """
    shared, store_a, store_b = await _two_writer_setup()
    await store_b.update_if_revision(1, {"displayName": "B 赢了"})

    request_revision = 1
    a_cache_revision = store_a.get().revision
    mongo_revision = shared.authoritative_revision
    assert a_cache_revision == 1, "前提：A 的进程内缓存确实陈旧"
    assert mongo_revision == 2, "前提：权威库确实已前进"

    client = await aiohttp_client(_app(store_a))
    status, body = await _put(client, expected_revision=request_revision)

    reported = body["details"].get("currentRevision")
    assert status == 409
    assert reported == mongo_revision, (
        f"request={request_revision} A缓存={a_cache_revision} Mongo={mongo_revision} "
        f"→ 409 currentRevision={reported!r}（必须是 {mongo_revision}）")
    assert reported != a_cache_revision, "不得把已知陈旧的缓存值当权威现值上报"


# ── Test CMT-3：多次 intervening write，`expected + 1` 假设必须被杀掉 ──


@pytest.mark.asyncio
async def test_cmt3_multiple_intervening_writes_not_expected_plus_one(aiohttp_client):
    """A 读 R1；B 写 R2；C 写 R3；A 用 R1 写 → 409 currentRevision 必须是 R3。

    `expected + 1` 会给出 2 —— 这条用例专门杀那个假设（还有缓存值 1）。
    """
    shared, store_a, store_b = await _two_writer_setup()
    store_c = UserIdentityStore(repo=UserIdentityRepository(db=shared.db_b))
    assert await store_c.connect(user_name="妖梦") is not None   # 第三个独立 writer

    await store_b.update_if_revision(1, {"displayName": "B 赢了"})
    await store_c.update_if_revision(2, {"displayName": "C 赢了"})
    assert shared.authoritative_revision == 3

    client = await aiohttp_client(_app(store_a))
    status, body = await _put(client, expected_revision=1)

    assert status == 409
    reported = body["details"].get("currentRevision")
    assert reported == 3, f"必须报权威现值 3，实际 {reported!r}（expected+1=2、缓存=1 都是假的）"


# ── Test CMT-4：失败 writer 不改变 winner 状态 ──


@pytest.mark.asyncio
async def test_cmt4_failed_writer_preserves_winner_state(aiohttp_client):
    """A 冲突后：库里仍是 winner 的内容与 revision —— 409 是报告失败，不是隐式重试。"""
    shared, store_a, store_b = await _two_writer_setup()
    await store_b.update_if_revision(1, {"displayName": "B 赢了", "pronouns": "他"})

    before = copy.deepcopy(shared.authoritative_doc)
    client = await aiohttp_client(_app(store_a))
    status, _body = await _put(client, expected_revision=1, display_name="A 的 payload")
    after = shared.authoritative_doc

    assert status == 409
    assert after == before, "失败 writer 的响应构造不得改动权威文档"
    assert shared.authoritative_doc["display_name"] == "B 赢了"
    assert shared.authoritative_doc["pronouns"] == "他"
    assert shared.authoritative_revision == 2


# ── Test CMT-5：健康更新不回归（ALREADY_GREEN_GUARD）──


@pytest.mark.asyncio
async def test_cmt5_healthy_update_still_succeeds(aiohttp_client):
    """expected revision 正确 → 200、revision 正常增长（并发保护未被削弱）。"""
    shared, store_a, _store_b = await _two_writer_setup()

    client = await aiohttp_client(_app(store_a))
    status, body = await _put(client, expected_revision=1, display_name="正常更新")

    assert status == 200
    assert body["displayName"] == "正常更新"
    assert body["revision"] == 2
    assert shared.authoritative_revision == 2
    # 赢家刷新自己的缓存是既有行为（本批不动）；冲突路径才禁止用缓存冒充权威
    assert store_a.get().revision == 2


# ── 顺序扰动：谁先写都必须报同一个权威现值（§53）──


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["A", "B"])
async def test_cmt_order_perturbation_both_orders_agree(aiohttp_client, winner):
    """A wins/B conflicts 与 B wins/A conflicts 两个顺序，409 都必须报权威现值。"""
    shared, store_a, store_b = await _two_writer_setup()
    first, second = (store_a, store_b) if winner == "A" else (store_b, store_a)

    await first.update_if_revision(1, {"displayName": f"{winner} 赢了"})
    assert shared.authoritative_revision == 2

    client = await aiohttp_client(_app(second))
    status, body = await _put(client, expected_revision=1)

    assert status == 409
    assert body["details"]["currentRevision"] == 2
    assert shared.authoritative_doc["display_name"] == f"{winner} 赢了"
    # 失败方缓存仍停在 R1（本批刻意不刷新，§38；将来做 E1 freshness 时这条判据要同步改）
    assert second.get().revision == 1


# ── 反复冲突：metadata 不得粘住第一次的值（§50 / Challenger Attack H）──


@pytest.mark.asyncio
async def test_cmt_repeated_conflict_tracks_authoritative_state(aiohttp_client):
    """A 连续用 R1 重试：库已 R3 再 R4 —— 每次 409 都必须反映当时的权威状态。"""
    shared, store_a, store_b = await _two_writer_setup()
    client = await aiohttp_client(_app(store_a))

    await store_b.update_if_revision(1, {"displayName": "B"})
    await store_b.update_if_revision(2, {"displayName": "C"})
    assert shared.authoritative_revision == 3

    first_status, first = await _put(client, expected_revision=1)
    assert first_status == 409 and first["details"]["currentRevision"] == 3

    await store_b.update_if_revision(3, {"displayName": "D"})
    second_status, second = await _put(client, expected_revision=1)

    assert second_status == 409
    assert second["details"]["currentRevision"] == 4, "不得粘住第一次冲突时的 3"


# ── 冲突 vs not found vs store unavailable（§31 / §32 / §33）──


@pytest.mark.asyncio
async def test_cmt_missing_record_reports_no_fabricated_revision(aiohttp_client):
    """权威记录不存在时：不得拿陈旧缓存的值冒充（fail-closed 承认"不知道"）。

    外部删除权威文档（缓存仍热）→ CAS 未命中且回读为空 ⇒ 409 保留，但
    `details.currentRevision` **缺省**（协议文档 §9.1 该字段本就"可带"）。
    """
    shared, store_a, _store_b = await _two_writer_setup()
    assert store_a.get().revision == 1           # 缓存热
    shared.db_a.collections[COLLECTION].clear()  # 外部写入者删掉权威文档

    client = await aiohttp_client(_app(store_a))
    status, body = await _put(client, expected_revision=1)

    assert status == 409
    assert body["code"] == "USER_PROFILE_REVISION_CONFLICT"
    assert "currentRevision" not in body["details"], (
        "记录不存在 ⇒ 权威现值未知，不得回退成缓存里的 1")


@pytest.mark.asyncio
async def test_cmt_read_failure_after_conflict_fails_closed_503(aiohttp_client):
    """冲突后权威回读失败 → 503（storage 不可用），不得退回陈旧值伪造 409。

    修前：路由根本不回读，直接拿缓存报 409 + currentRevision=1（假真相）。
    """
    shared, store_a, store_b = await _two_writer_setup(fail_a_read=True)
    assert store_a.is_available is True          # 种子阶段一切正常
    await store_b.update_if_revision(1, {"displayName": "B 赢了"})

    fail_db = shared.db_a
    assert isinstance(fail_db, _ReadFailAfterSeedDB)
    fail_db.fail_find = True                     # 只让 A 的权威回读失败

    client = await aiohttp_client(_app(store_a))
    status, body = await _put(client, expected_revision=1)

    assert status == 503, f"回读失败必须 fail-closed，实际 {status} / {body}"
    assert body["code"] == "STORAGE_UNAVAILABLE"
    assert body["retryable"] is True
    assert "currentRevision" not in body["details"]


# ── 缺 / null / 非整数 / 非法 revision 在 request validation 阶段就被挡住（§30）──


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_expected", [0, -1, "abc", None, 1.5])
async def test_cmt_invalid_expected_revision_never_reaches_conflict_path(
        aiohttp_client, bad_expected):
    """`expectedRevision` 缺域/错类型 → 400（`ge=1` + int 契约），**不**走 409 元数据路径。

    本批不扩大 validation scope；这条只钉住"坏请求不会被冲突修复逻辑接管"。
    """
    shared, store_a, _store_b = await _two_writer_setup()
    body = dict(_UPDATE, displayName="x", expectedRevision=bad_expected)

    client = await aiohttp_client(_app(store_a))
    resp = await client.put("/api/me", json=body)
    payload = await resp.json()

    assert resp.status == 400, payload
    assert payload["code"] == "CHARACTER_VALIDATION"
    assert shared.authoritative_revision == 1, "坏请求不得改动权威状态"


@pytest.mark.asyncio
async def test_cmt_missing_expected_revision_is_validation_error(aiohttp_client):
    """整个 `expectedRevision` 键缺失 → 400（契约必填），同样不进冲突路径。"""
    shared, store_a, _store_b = await _two_writer_setup()
    body = dict(_UPDATE, displayName="x")
    body.pop("expectedRevision", None)

    client = await aiohttp_client(_app(store_a))
    resp = await client.put("/api/me", json=body)

    assert resp.status == 400
    assert shared.authoritative_revision == 1
