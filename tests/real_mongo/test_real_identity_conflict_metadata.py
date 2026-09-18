# 测试层：tests/real_mongo/test_real_identity_conflict_metadata.py
"""真实 MongoDB 层：409 `currentRevision` 在**真 CAS** 与**真 HTTP 路由**上是否等于权威现值（Batch 6 / N-3）。

## 为什么需要真库（本层纪律：只挑 fake 回答不了的问题）

`test_me_conflict_metadata_authority.py` 用 `FakeDocumentDBBackend` 证明冲突元数据取自
权威回读。但那个结论依赖两件事只有真驱动能回答：

1. 真 `update_one(filter={user_id, revision: expected})` 的 **matched/modified_count
   语义**：CAS 未命中时真的返回 0，而命中时真的写进去了（fake 是照这份语义手写的，
   真库才是它的**判据来源**）；
2. 两条**独立连接**（两个 repository 实例）打同一个库时，落败方回读到的确实是
   winner 已经提交的文档（fake 里那是共享 dict，真库里是服务端状态）。

额外一条本层才有价值的证据：winner 由一个**全新子进程**（新解释器、新 store、新连接）
写入——排除"同一个进程内的对象状态造成假象"（Batch 6 §54）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web

from core.input.character_routes import register_character_routes
from core.profile.profile_store import UserIdentityStore
from core.storage.user_identity_repo import COLLECTION, UserIdentityRepository

pytestmark = [
    pytest.mark.external,      # opt-in 层：默认任何档位都不跑（Normal = not external）
    pytest.mark.real_mongo,    # 本层专属 marker：CI 的 real-mongo job 用 -m real_mongo 选中
]

_REPO_ROOT = Path(__file__).resolve().parents[2]


async def _two_real_stores(real_mongo_repo) -> tuple[UserIdentityStore, UserIdentityStore]:
    """两个独立 repository（两条真连接）+ 两个独立 store，指向同一个真实测试库。"""
    repo_a = await real_mongo_repo(UserIdentityRepository)
    repo_b = await real_mongo_repo(UserIdentityRepository)
    store_a = UserIdentityStore(repo=repo_a)
    store_b = UserIdentityStore(repo=repo_b)
    assert await store_a.connect(user_name="妖梦") is not None
    assert await store_b.connect(user_name="妖梦") is not None
    return store_a, store_b


async def _authoritative_revision(backend) -> int:
    docs = await backend.find(COLLECTION, {"user_id": "usr_local_default"}, limit=1)
    assert docs, "权威文档应当存在"
    return int(docs[0]["revision"])


def _app(store: UserIdentityStore) -> web.Application:
    app = web.Application()
    app["user_identity_store"] = store
    register_character_routes(app)
    return app


@pytest.mark.asyncio
async def test_real_conflict_reports_authoritative_revision(
        aiohttp_client, real_mongo_backend, real_mongo_repo):
    """真库 + 真路由：A/B 都在 R1；B 真 CAS 到 R2；A 用 R1 → 409 currentRevision=2。

    三个数字全部来自真实现：A 的**进程内缓存**（仍 1）、**真库文档**（2）、409 的
    `details.currentRevision`（必须 2）。
    """
    store_a, store_b = await _two_real_stores(real_mongo_repo)
    assert store_a.get().revision == 1 and store_b.get().revision == 1
    assert await _authoritative_revision(real_mongo_backend) == 1

    await store_b.update_if_revision(1, {"displayName": "B 赢了"})
    assert await _authoritative_revision(real_mongo_backend) == 2

    # A 的缓存按定义已经陈旧（本批刻意不刷新缓存）
    assert store_a.get().revision == 1

    client = await aiohttp_client(_app(store_a))
    resp = await client.put("/api/me", json={
        "displayName": "A 的 payload", "expectedRevision": 1,
        "locale": "zh-CN", "timezone": "Asia/Shanghai",
    })
    body = await resp.json()

    assert resp.status == 409
    assert body["code"] == "USER_PROFILE_REVISION_CONFLICT"
    assert body["details"]["currentRevision"] == 2, (
        f"真库已 R2，409 报 {body['details'].get('currentRevision')!r}（A 缓存="
        f"{store_a.get().revision}）")
    assert await _authoritative_revision(real_mongo_backend) == 2
    docs = await real_mongo_backend.find(COLLECTION, {"user_id": "usr_local_default"},
                                         limit=1)
    assert docs[0]["display_name"] == "B 赢了", "失败 writer 不得改动 winner 状态"


@pytest.mark.asyncio
async def test_real_multiple_writes_never_expected_plus_one(
        aiohttp_client, real_mongo_backend, real_mongo_repo):
    """真库上 R1→R2→R3：A 用 R1 冲突 → 409 必须是 3（`expected + 1` 只会给 2）。"""
    store_a, store_b = await _two_real_stores(real_mongo_repo)
    await store_b.update_if_revision(1, {"displayName": "B"})
    await store_b.update_if_revision(2, {"displayName": "C"})
    assert await _authoritative_revision(real_mongo_backend) == 3

    client = await aiohttp_client(_app(store_a))
    resp = await client.put("/api/me", json={
        "displayName": "A", "expectedRevision": 1,
        "locale": "zh-CN", "timezone": "Asia/Shanghai",
    })
    body = await resp.json()

    assert resp.status == 409
    assert body["details"]["currentRevision"] == 3


_CHILD_WRITE = """
import asyncio
import sys

from core.profile.profile_store import UserIdentityStore
from core.storage.user_identity_repo import UserIdentityRepository


async def main() -> None:
    db_name, uri, expected, name = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    repo = UserIdentityRepository(database_name=db_name, base_url=uri)
    if not await repo.connect():
        print("CONNECT_FAILED")
        raise SystemExit(2)
    store = UserIdentityStore(repo=repo)
    await store.connect(user_name="妖梦")
    await store.update_if_revision(expected, {"displayName": name})
    # 判据取自**权威库**（而非返回类型形状），使本用例在修前/修后跑同一段判据
    fresh = await repo.get("usr_local_default")
    print("APPLIED" if fresh is not None and fresh.revision == expected + 1 else "CONFLICT")
    await repo.close()


asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_real_cross_process_writer_is_the_reported_authority(
        aiohttp_client, real_mongo_uri, real_mongo_db_name, real_mongo_backend,
        real_mongo_repo):
    """**跨进程**：winner 由全新子进程（新解释器/新 store/新连接）写入 → 409 报它的 revision。

    这条排除"同进程共享对象状态"这一整类假象（§54）：父进程的 store 从未见过子进程的
    写入，唯一能知道 R2 的途径就是**回读权威库**。
    """
    store_a, _store_b = await _two_real_stores(real_mongo_repo)
    # 父进程内先建立一个"真实的旧状态"：子进程将在这个 revision 基础上写入
    assert store_a.get().revision == 1

    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_WRITE, real_mongo_db_name, real_mongo_uri,
         "1", "子进程赢家"],
        cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"子进程写入失败：{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip().endswith("APPLIED"), proc.stdout
    assert await _authoritative_revision(real_mongo_backend) == 2

    client = await aiohttp_client(_app(store_a))
    resp = await client.put("/api/me", json={
        "displayName": "父进程 payload", "expectedRevision": 1,
        "locale": "zh-CN", "timezone": "Asia/Shanghai",
    })
    body = await resp.json()

    assert resp.status == 409
    assert body["details"]["currentRevision"] == 2, (
        "跨进程 winner 的 revision 必须由权威回读得到（父进程缓存里根本没有它）")
    docs = await real_mongo_backend.find(COLLECTION, {"user_id": "usr_local_default"},
                                         limit=1)
    assert docs[0]["display_name"] == "子进程赢家"
