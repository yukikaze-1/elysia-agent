# core/profile/tests/test_ready_identity_truthfulness.py
"""Batch 4（N-1 + N-11）——运行时健康真值：装载失败 → 运行时状态 → readiness 投影。

## 本文件钉住的因果链

    真实装载失败（repo 通、身份文档不合域模型）
            ↓  ①  必须由 store 持有为可查状态（N-11：不再只有一行 warning）
    UserIdentityStore.is_available == False（+ last_error 可查）
            ↓  ②  产品入口与 /ready 必须读同一个权威
    /api/me → 503        /ready → 503 chatReady=false（N-1：不再假绿）

## 为什么这些用例必须走真实对象

§37/§48 纪律：**不能只测 helper，也不能靠手工置位**。此前 `test_health.py` 的
identity 用例是把 `agent.user_identity_store.health_check` 打成 False 再断言 503
——那证明的是「门禁接到了某个可翻转的谓词」，**不**能证明「真实装载失败会让那个谓词
变假」。`is_available` 恒 True 而 `health_check()` 恒 False/True 的错配正是 N-1 本体，
故本文件的判据全部从**真实 store + 真实 UserIdentityRepository + 真实 create_app 路由**
里长出来，绝不手工改 `is_available`。

## 状态口径（§68）

- PRE_FIX_RED：修前必红（N-11 的 `last_error` 不存在、`/ready` 读 ping 假绿）；
- ALREADY_GREEN_GUARD：修前已绿，防回归用。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.agent import RunState
from core.profile.profile_store import UserIdentityStore
from core.storage.base import StorageUnavailableError
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.storage.user_identity_repo import UserIdentityRepository

pytestmark = pytest.mark.service

REPO_ROOT = Path(__file__).resolve().parents[3]


# ── 受控故障注入：Mongo 可达，但身份文档装载失败 ──────────────────────────────


class _UndecodableDocRepo(UserIdentityRepository):
    """ping 通（`health_check()` True）、`get()` 抛错——真实故障形态之一。

    对应库里存在一条不合当前域模型的记录（B3-CB-1 的 `pronouns: ""` 曾是活例）：
    网络没问题、Mongo 没问题、`repo.is_connected` 为真，只有**反序列化**失败。
    这正是「ping 通 ≠ 身份可用」的最小形态（§44 Challenger Attack B）。
    """

    async def get(self, user_id: str):
        raise ValueError("模拟反序列化失败（文档不合当前域模型）")


class _DownRepo(UserIdentityRepository):
    """Mongo 完全不可达——`connect()` 失败。"""

    async def connect(self) -> bool:
        return False


def _store_with_load_failure() -> UserIdentityStore:
    """真实 store（装配真实 repo）跑一次**真实** `connect()` → 装载失败。"""
    return UserIdentityStore(repo=_UndecodableDocRepo(db=FakeDocumentDBBackend()))


def _mock_agent(store: UserIdentityStore, *, milvus_ok: bool = True) -> MagicMock:
    """健康端点用 stub agent——**只**替身与身份无关的子系统，身份走真实对象。"""
    from unittest.mock import AsyncMock

    agent = MagicMock()
    agent.status.current_name.return_value = "idle"
    agent.llm.model = "test-model"
    agent.session.messages = []
    agent.session.health_check = AsyncMock(return_value=True)
    agent.long_term.count = 0
    agent.long_term.health_check = AsyncMock(return_value=milvus_ok)
    agent.long_term.embedding_ready = True
    agent.message_repo.health_check = AsyncMock(return_value=True)
    agent.attachment_repo.health_check = AsyncMock(return_value=True)
    agent.tool_adapter.tool_names = []
    # 与 create_app 一致：REST 与探针复用**同一个** store（真实 store，非替身）
    agent.user_identity_store = store
    agent._state = RunState.RUNNING
    agent.state = RunState.RUNNING
    return agent


def _app_and_client(store: UserIdentityStore, *, milvus_ok: bool = True):
    """真实 create_app + 真实路由（aiohttp TestClient）。"""
    from aiohttp.test_utils import TestClient, TestServer

    from core.input.server import create_app

    app = create_app(_mock_agent(store, milvus_ok=milvus_ok), serve_static=False)
    return TestClient(TestServer(app))


def _seed_identity():
    """权威库里的一份合法档案（`usr_local_default` 是 V1 单用户常量）。"""
    from core.profile.user_identity import UserIdentity

    return UserIdentity(userId="usr_local_default", displayName="妖梦")


# ══════════════════════════════════════════════════════════════════════
# RHT-1 / N-11：装载失败必须留下运行时状态（不是只有日志）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_RHT1_load_failure_sets_unavailable_and_queryable_error():
    """RHT-1（PRE_FIX_RED）：真实装载失败 → 运行时**可查**的不可用状态。

    修前：`connect()` 只 `logger.warning`，无任何可查询的失败表示
    （`last_error` 属性不存在 → AttributeError → 本用例红）。
    修后：`is_available` False（可用性权威）+ `last_error` 为异常类型名（诊断）。
    """
    store = _store_with_load_failure()

    loaded = await store.connect(user_name="妖梦")   # 真实装载路径，真实失败

    assert loaded is None
    assert store.get() is None
    assert store.is_authoritative is True
    # ① 可用性权威如实为假——产品路径据此 fail-closed
    assert store.is_available is False
    # ② 失败原因可查（不靠日志文本；日志会被吞、被降级、被采样）
    assert store.last_error == "ValueError", (
        "装载失败的类型必须留在运行时状态里，否则「为什么不可用」只能靠翻日志")
    # ③ 失败与不可用同源：不允许「有失败记录但报可用」的分叉
    assert (store.last_error is not None) == (not store.is_available)


@pytest.mark.asyncio
async def test_RHT1b_repo_ping_ok_does_not_imply_identity_available():
    """RHT-1 的**判据分离**（PRE_FIX_RED）：ping 通 ≠ 身份可用。

    这是 N-1 的根因断言：两个谓词回答两个不同问题，谁也不能冒充谁。
    若有人把 `/ready`（或任何消费方）改回读 `health_check()`，本用例给出的
    「ping True / is_available False」组合就是那个改动的反例。
    """
    store = _store_with_load_failure()
    await store.connect(user_name="妖梦")

    assert await store.health_check() is True, "repo 本身是可达的（本用例的前提）"
    assert store.get() is None
    assert store.is_available is False, (
        "权威库可达但身份未装载——此时身份子系统**不可用**")


# ══════════════════════════════════════════════════════════════════════
# RHT-2 / N-1：身份不可用 → /ready 不得假绿
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_RHT2_ready_503_when_identity_load_failed(aiohttp_client):
    """RHT-2（PRE_FIX_RED）：真实装载失败 → 真实 `/ready` 路由 503。

    修前实测：同一受控状态下 `/ready` 200 `chatReady=true`（identity 门禁探的是
    ping → True），而 `/api/me` 503——部署围栏与产品围栏互相矛盾。
    """
    store = _store_with_load_failure()
    await store.connect(user_name="妖梦")

    async with _app_and_client(store) as client:
        resp = await client.get("/ready")
        data = await resp.json()

    assert resp.status == 503, f"身份不可用时 /ready 必须摘除，实得 {resp.status}"
    assert data["chatReady"] is False
    assert data["dependencies"]["identity"] is False
    assert data["dependencies"]["lifecycle"] is True   # 进程仍在跑（不是 liveness 故障）
    assert data["dependencies"]["mongo"] is True       # 也不是存储可达性故障
    assert data["status"] == "degraded"


@pytest.mark.asyncio
async def test_RHT3_ready_200_when_identity_loaded(aiohttp_client):
    """RHT-3（ALREADY_GREEN_GUARD）：健康路径仍就绪——修复不得把好路径一起判死。"""
    db = FakeDocumentDBBackend()
    repo = UserIdentityRepository(db=db)
    await repo.connect()
    store = UserIdentityStore(repo=repo)
    loaded = await store.connect(user_name="妖梦")
    assert loaded is not None

    async with _app_and_client(store) as client:
        resp = await client.get("/ready")
        data = await resp.json()

    assert resp.status == 200
    assert data["chatReady"] is True
    assert data["fullReady"] is True
    assert data["dependencies"]["identity"] is True
    assert "last_error" not in data, "诊断细节不外泄到 /ready（§22/§50）"


# ══════════════════════════════════════════════════════════════════════
# RHT-3'（§36 最重要的 cross-surface 不变量）：产品入口与 /ready 同真同假
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("identity_loaded", [True, False])
@pytest.mark.asyncio
async def test_RHT3b_ready_and_product_endpoint_agree(aiohttp_client, identity_loaded):
    """§36：产品入口因身份不可用而 503 时，`/ready` **不得**声称 chatReady=true（反之亦然）。

    两个 surface 在**同一时刻、同一 store** 下取值，禁止"分别测两个状态"。
    """
    db = FakeDocumentDBBackend()
    repo = UserIdentityRepository(db=db) if identity_loaded else _UndecodableDocRepo(db=db)
    await repo.connect()
    if identity_loaded:
        await repo.bootstrap_if_absent(_seed_identity())
    store = UserIdentityStore(repo=repo)
    await store.connect(user_name="妖梦")

    async with _app_and_client(store) as client:
        ready = await client.get("/ready")
        ready_data = await ready.json()
        me = await client.get("/api/me")
        me_status = me.status
        me_data = await me.json()

    if not identity_loaded:
        assert me_status == 503 and me_data["code"] == "STORAGE_UNAVAILABLE"
    assert ready_data["chatReady"] is (me_status == 200), (
        f"/ready chatReady={ready_data['chatReady']} 与产品入口 /api/me HTTP {me_status} 矛盾")


# ══════════════════════════════════════════════════════════════════════
# RHT-4 / RHT-5：本地 fallback 的真实语义（load-bearing catch 未被误删）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_RHT4_local_bootstrap_path_is_not_misjudged_unavailable(tmp_path):
    """RHT-4（ALREADY_GREEN_GUARD）：无 repo 的本地模式**仍可用**，不因缺文件判死。

    本地模式的 `_load_from_file()` fallback 是既定支持路径（`storage_path` 未配置时
    直接 bootstrap）：文件不存在 → 新建身份 → 可用。
    """
    path = tmp_path / "profile.json"
    store = UserIdentityStore(storage_path=str(path))

    ui = await store.connect(user_name="妖梦")

    assert ui is not None and ui.displayName == "妖梦"
    assert store.is_available is True
    assert store.last_error is None
    assert json.loads(path.read_text("utf-8"))["displayName"] == "妖梦"


@pytest.mark.asyncio
async def test_RHT5_local_corrupt_cache_is_recorded_then_cleared_by_recovery(tmp_path):
    """RHT-5（PRE_FIX_RED）：本地缓存文件损坏 → 失败**不被完全吞掉**。

    `_load_from_file` 的 catch 是**故意保留**的降级（load-bearing）：它后面仍有
    有效状态（新建身份 + `_persist` 覆盖损坏文件），故可用性**如实**保持 True
    ——降级成功不是不可用。要钉的是两件事：① 失败留下了可查记录（修前
    `last_error` 不存在）；② 恢复成功后被消解（§21：失败历史不得钉死当前判定）。
    """
    path = tmp_path / "profile.json"
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")
    store = UserIdentityStore(storage_path=str(path))

    ui = store.bootstrap(user_name="妖梦")

    assert ui.displayName == "妖梦"                    # 降级成功，服务可用
    assert store.is_available is True
    assert store.last_error is None, (
        "bootstrap 已用合法身份覆盖损坏文件 → 当前状态可信，失败记录必须被消解")
    # 缓存文件已被合法文档覆盖 → 全新进程读回成功（不是永久脏状态）
    fresh = UserIdentityStore(storage_path=str(path))
    assert fresh.get() is not None
    assert fresh.last_error is None


@pytest.mark.asyncio
async def test_RHT5a_local_corrupt_cache_records_error_before_recovery(tmp_path):
    """RHT-5 的记录面：损坏文件被读到时**必须**留下失败记录（不是静默 None）。

    注：本地模式只有 `bootstrap()` 有"修复"这一步（重建身份 + 覆盖文件）；
    `get()` 是纯读路径，读失败只报 None——本批不改该语义（无生产调用方传
    `storage_path`，见 §75 记录）。
    """
    path = tmp_path / "profile.json"
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")
    store = UserIdentityStore(storage_path=str(path))

    assert store._load_from_file() is None          # noqa: SLF001 —— 直接观测该路径
    assert store.last_error == "JSONDecodeError", "解析失败必须留下类型名，不能只有 warning"

    repaired = store.bootstrap(user_name="妖梦")     # 修复路径
    assert repaired.displayName == "妖梦"
    assert store.last_error is None, "修复后失败记录必须被消解"
    assert UserIdentityStore(storage_path=str(path)).get() is not None   # 文件已可读


@pytest.mark.asyncio
async def test_RHT5b_authoritative_failure_has_no_fallback_so_unavailable():
    """RHT-5 的另一半：权威模式下**没有** fallback 可用 → 可用性必须为假。

    与上一条构成对照：同样是"读失败"，本地模式有有效兜底（可用），权威模式没有
    （不可用）——可用性由**最终状态**决定，而不是由"有没有抛异常"决定。
    """
    store = _store_with_load_failure()
    await store.connect(user_name="妖梦")

    assert store.get() is None
    assert store.is_available is False
    assert store.last_error is not None


# ══════════════════════════════════════════════════════════════════════
# RHT-6 / §46：恢复语义按现有 lifecycle（不新增 recovery 框架）
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_RHT6_reconnect_clears_failure_and_restores_ready(aiohttp_client):
    """RHT-6：现有 lifecycle 的恢复路径是 `connect()` 重载（恢复协调器已接线）。

    第一次装载失败 → 不可用；同一 store 再次 `connect()`（对应
    `_recover_dependency("identity")`）→ 可用、失败记录清空、`/ready` 恢复 200。
    §21：失败历史不得让可用性永久为假。
    """
    class _FlakyRepo(UserIdentityRepository):
        def __init__(self, db) -> None:
            super().__init__(db=db)
            self.broken = True

        async def get(self, user_id: str):
            if self.broken:
                raise ValueError("模拟反序列化失败")
            return await super().get(user_id)

    db = FakeDocumentDBBackend()
    repo = _FlakyRepo(db=db)
    await repo.connect()
    # 先在"健康"状态把手写文档落库（bootstrap_if_absent 内部会 get 读回）
    repo.broken = False
    await repo.bootstrap_if_absent(_seed_identity())
    repo.broken = True
    store = UserIdentityStore(repo=repo)

    assert await store.connect(user_name="妖梦") is None       # 第一程：失败
    assert store.is_available is False
    assert store.last_error == "ValueError"
    async with _app_and_client(store) as client:
        assert (await client.get("/ready")).status == 503

    repo.broken = False                                        # Mongo/文档已修复
    loaded = await store.connect(user_name="妖梦")             # 恢复协调器的动作

    assert loaded is not None and loaded.displayName == "妖梦"
    assert store.is_available is True
    assert store.last_error is None, "恢复后必须清空失败记录（否则 readiness 会被历史钉死）"
    async with _app_and_client(store) as client:
        resp = await client.get("/ready")
        assert resp.status == 200
        assert (await resp.json())["chatReady"] is True


# ══════════════════════════════════════════════════════════════════════
# §67 fresh process：restart 重新决定可用性，不继承上一次的坏状态
# ══════════════════════════════════════════════════════════════════════


_FRESH_PROCESS_PROBE = r'''
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from core.input.server import create_app
from core.agent import RunState
from core.profile.profile_store import UserIdentityStore
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.storage.user_identity_repo import UserIdentityRepository
from unittest.mock import AsyncMock, MagicMock
from aiohttp.test_utils import TestClient, TestServer

class BadRepo(UserIdentityRepository):
    async def get(self, user_id):
        raise ValueError("bad doc")

def mock_agent(store):
    a = MagicMock()
    a.status.current_name.return_value = "idle"
    a.llm.model = "m"
    a.session.messages = []
    a.session.health_check = AsyncMock(return_value=True)
    a.long_term.count = 0
    a.long_term.health_check = AsyncMock(return_value=True)
    a.long_term.embedding_ready = True
    a.message_repo.health_check = AsyncMock(return_value=True)
    a.attachment_repo.health_check = AsyncMock(return_value=True)
    a.tool_adapter.tool_names = []
    a.user_identity_store = store
    a._state = RunState.RUNNING
    a.state = RunState.RUNNING
    return a

async def run():
    mode = sys.argv[2]
    db = FakeDocumentDBBackend()
    repo = BadRepo(db=db) if mode == "bad" else UserIdentityRepository(db=db)
    await repo.connect()
    store = UserIdentityStore(repo=repo)
    await store.connect(user_name="u")
    app = create_app(mock_agent(store), serve_static=False)
    async with TestClient(TestServer(app)) as c:
        r = await c.get("/ready")
        body = await r.json()
    print(f"RESULT mode={mode} status={r.status} chatReady={body['chatReady']} "
          f"available={store.is_available}")

asyncio.run(run())
'''


def _run_fresh_process(mode: str) -> str:
    """在**全新子进程**里跑一次真实 create_app + 真实 /ready（§39/§91）。"""
    proc = subprocess.run(
        [sys.executable, "-c", _FRESH_PROCESS_PROBE, str(REPO_ROOT), mode],
        capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, f"fresh 子进程失败：{proc.stderr[-2000:]}"
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
    assert line, f"子进程无 RESULT 行：{proc.stdout[-2000:]}"
    return line[0]


def test_RHT7_fresh_process_derives_availability_from_its_own_load():
    """§67：新进程**重新**装载并重新决定可用性——不继承上一次的坏状态。

    两个 fresh 进程（坏/好）各自从零装载：坏的 not-ready、好的 ready。若有人把
    "上次运行坏了"持久化下来，第二条会先天 not-ready，本用例即红。
    """
    bad = _run_fresh_process("bad")
    good = _run_fresh_process("good")

    assert "status=503" in bad and "chatReady=False" in bad and "available=False" in bad
    assert "status=200" in good and "chatReady=True" in good and "available=True" in good


# ══════════════════════════════════════════════════════════════════════
# §60：「保持原职责」的反向守卫——health_check 仍是恢复协调器探针
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_keeps_recovery_coordinator_semantics():
    """KEEP_AS_IS：`health_check()` 仍是"权威库可达性"（恢复协调器探针），未被本批改变。

    它**不**是 readiness 判据（见 RHT-1b），但仍是 `_probe_recovery` 的输入——
    本批若把它改成"装载结果"会让恢复协调器永远认为库不可达。
    """
    up = _store_with_load_failure()             # repo 可达、装载失败
    assert await up.health_check() is True

    down = UserIdentityStore(repo=_DownRepo())
    assert await down.health_check() is False   # 库不可达
    assert await UserIdentityStore().health_check() is True   # 本地模式不门禁


@pytest.mark.asyncio
async def test_recovery_predicate_matches_readiness_authority():
    """§55 运行时权威一致性：修复器的判据与 `/ready` 的判据必须同源。

    `_dependency_needs_recovery("identity")` 本就是 `not store.is_available`；
    `/ready` 现在读同一个属性——两者不允许再分叉。
    """
    from core.agent.tests.test_probe_recovery_paths import _make_agent

    agent = _make_agent()
    store = _store_with_load_failure()
    await store.connect(user_name="妖梦")
    agent._user_identity_store = store

    assert agent._dependency_needs_recovery("identity") is True
    assert store.is_available is False

    # 反向：可用时不得要求补偿（否则每轮空转重载）
    db = FakeDocumentDBBackend()
    repo = UserIdentityRepository(db=db)
    await repo.connect()
    ok_store = UserIdentityStore(repo=repo)
    await ok_store.connect(user_name="妖梦")
    agent._user_identity_store = ok_store
    assert agent._dependency_needs_recovery("identity") is False


def test_unavailable_is_exact_bool():
    """判据是**真布尔**：`is True` 比较必须有意义（防 Mock/truthy 冒充健康）。

    `health.py` 用 `is True` 而非真值判断，是为了让"门禁接到了 Mock 属性"
    这件事立刻可见，而不是被 MagicMock 的真值悄悄放过。
    """
    assert UserIdentityStore().is_available is True
    assert _store_with_load_failure().is_available is False


@pytest.mark.asyncio
async def test_unavailable_store_still_fails_closed_on_write():
    """反向守卫：不可用时写路径仍 fail-closed（既有 P0-3 语义未被本批破坏）。"""
    store = _store_with_load_failure()
    await store.connect(user_name="妖梦")

    with pytest.raises(StorageUnavailableError):
        await store.update_if_revision(1, {"displayName": "妖梦2"})
