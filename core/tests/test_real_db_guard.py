"""真实数据库访问防线的单元测试（marker=unit）。

覆盖 `core/tests/_real_db_guard.py` 的三个可测构件：
- `is_exempt`：marker 豁免判定（integration/slow/external 允许连真实库）
- `make_blocked_client`：替换真实客户端的桩——**构造成功、任何操作即抛并记录**
- `install_fake_mongo` / `install_test_milvus_collection`：两个导流/改名装置

## 端到端效果在哪里被验证（2026-09-28 更正）

**本文件此前写的是"由 `core/tests/test_real_db_guard_e2e.py` 以子进程验证"——
那个文件从来不存在**，是一个指向不存在测试的失效引用。实际分布是：

| 判据 | 在哪 |
|------|------|
| 装置本身的行为（导流/改名/幂等/豁免表） | **本文件**（`TestInstallFakeMongo` / `TestInstallTestMilvusCollection`） |
| **autouse 接线**（根 conftest 真的把它们装到了用例上吗） | 本文件 `TestAutouseWiring`——在**会话内**断言，不需要子进程 |
| 协议帧在坏 backend 下的可达性 | `core/input/tests/test_v2_ws.py`（与防线无关，同属"必须帧不许消失"族） |
| 并行跑共用真实资源被拒答 | `core/tests/test_parallel_guard_e2e.py`（真子进程，因为它要跑**真实 CLI 会话**） |

**为什么不需要子进程**：防线的失败形态是**导流**（不是 `pytest.fail`），所以在同一个
会话里直接断言"这次用例的工厂已被换掉"就足够了。"必须起新会话才验得了"的只有
"整轮被拒答"那条（守卫用 `pytest.fail` + `shouldstop`）——那才是
`test_parallel_guard_e2e.py` 存在的理由。
"""

from __future__ import annotations

import pytest

from core.tests._real_db_guard import (
    EXEMPT_MARKERS,
    RealDatabaseAccessError,
    install_fake_mongo,
    install_test_milvus_collection,
    is_exempt,
    make_blocked_client,
    production_mongo_provider_ids,
)

pytestmark = pytest.mark.unit

#: 防线装置的宿主模块——用它判定"根 conftest 是否真的装上了"。
_GUARD_MODULE = "core.tests._real_db_guard"


class TestIsExempt:
    """哪些 marker 允许连真实库。"""

    def test_no_marker_not_exempt(self):
        assert is_exempt([]) is False

    def test_unit_not_exempt(self):
        assert is_exempt(["unit"]) is False

    def test_service_not_exempt(self):
        assert is_exempt(["service"]) is False

    def test_integration_exempt(self):
        assert is_exempt(["integration"]) is True

    def test_slow_exempt(self):
        assert is_exempt(["slow"]) is True

    def test_external_exempt(self):
        assert is_exempt(["external"]) is True

    def test_exempt_wins_over_unit(self):
        """同时标 unit + slow（历史分层叠加）→ 允许真库。"""
        assert is_exempt(["unit", "slow"]) is True

    def test_exempt_markers_is_the_documented_set(self):
        assert EXEMPT_MARKERS == frozenset({"integration", "slow", "external"})


class TestBlockedClient:
    """桩客户端：构造不抛，操作抛 + 记录。"""

    def test_construction_is_allowed(self):
        """构造必须成功——否则会误伤只建后端、不做 I/O 的测试。"""
        client_cls = make_blocked_client(lambda _w: None, "Stub")
        client_cls("mongodb://localhost:27017", serverSelectionTimeoutMS=1)

    def test_nested_operation_records_and_raises(self):
        """`client[db][coll].insert_one(...)` 形态：抛错且记录。"""
        seen: list[str] = []
        client_cls = make_blocked_client(seen.append, "Stub")
        client = client_cls("mongodb://localhost:27017")

        with pytest.raises(RealDatabaseAccessError):
            client["default"]["sessions"].insert_one({"a": 1})
        assert seen, "访问尝试必须被记录（供 teardown 判定）"
        assert "insert_one" in seen[0]

    def test_attribute_operation_records_and_raises(self):
        """`client.admin.command(...)` 形态：同样拦截。"""
        seen: list[str] = []
        client_cls = make_blocked_client(seen.append, "Stub")
        client = client_cls("mongodb://localhost:27017")

        with pytest.raises(RealDatabaseAccessError):
            client.admin.command("ping")
        assert seen

    def test_error_message_names_the_display_name(self):
        """错误信息要带客户端名，便于定位。"""
        client_cls = make_blocked_client(lambda _w: None, "MilvusClient")
        client = client_cls()

        with pytest.raises(RealDatabaseAccessError, match="MilvusClient"):
            client.describe_collection("x")

    def test_error_message_gives_actionable_guidance(self):
        """错误信息要给出修法（注入 fake / 改标 integration）。"""
        client_cls = make_blocked_client(lambda _w: None, "Stub")
        client = client_cls()

        with pytest.raises(RealDatabaseAccessError, match="integration"):
            client.list_collections()


class TestProductionIds:
    """生产库标识集合。"""

    def test_contains_hardcoded_repo_literal(self):
        """仓储 connect() 硬编码 `mongodb/default`——必须在集合内。"""
        assert "mongodb/default" in production_mongo_provider_ids()

    def test_contains_configured_db_name(self):
        from core.config import AppConfig

        assert f"mongodb/{AppConfig().database.db_name}" in production_mongo_provider_ids()


class TestInstallFakeMongo:
    """`install_fake_mongo`：把生产库连接重定向到内存 fake。"""

    def test_production_id_redirected_to_fake(self, monkeypatch):
        from Module.database.documentdb.registry import DocumentDBFactory
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        seen: list[str] = []
        install_fake_mongo(monkeypatch, seen.append)

        backend = DocumentDBFactory.create("mongodb/default")
        assert isinstance(backend, FakeDocumentDBBackend)
        assert seen, "命中生产库必须记录（供 teardown 判定违规）"

    def test_non_production_uri_not_redirected(self, monkeypatch):
        """测试显式把 MONGODB_URI 改到别处（如不可达端口测失败分支）→ 不导流。"""
        from Module.database.documentdb.registry import DocumentDBFactory

        monkeypatch.setenv("MONGODB_URI", "mongodb://127.0.0.1:1")
        seen: list[str] = []
        install_fake_mongo(monkeypatch, seen.append)

        backend = DocumentDBFactory.create("mongodb/default")
        assert type(backend).__name__ == "MongoDBBackend"
        assert seen == []

    def test_non_production_id_delegates_to_real_factory(self, monkeypatch):
        """工厂自身测试用的标识（如 mongodb/elysia）不受影响。"""
        from Module.database.documentdb.registry import DocumentDBFactory

        # 真工厂需要 MONGODB_URI；motor 客户端是惰性的，构造不产生 I/O。
        monkeypatch.setenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
        seen: list[str] = []
        install_fake_mongo(monkeypatch, seen.append)

        backend = DocumentDBFactory.create("mongodb/elysia")
        assert type(backend).__name__ == "MongoDBBackend"
        assert seen == []


class TestInstallTestMilvusCollection:
    """`install_test_milvus_collection`：生产 LTM 集合对测试不可见。

    背景（2026-09-11 实测）：`MemoryRepository.clear_all()` 是 **drop + recreate
    collection**，`TestDebugDbApi::test_ltm_clear_all` 因此在真实 Milvus 上清空了整个
    `long_term_memory_v2`。重定向后任何测试都只作用于 `*__test` 集合。
    """

    def test_redirects_both_collections(self, monkeypatch):
        """两个仓储类的 COLLECTION 都以 `__test` 结尾（autouse 防线已在跑）。"""
        from core.storage.memory_repo import MemoryRepository
        from core.storage.memory_repo_v2 import MemoryRepositoryV2

        install_test_milvus_collection(monkeypatch)
        assert MemoryRepository.COLLECTION.endswith("__test")
        assert MemoryRepositoryV2.COLLECTION.endswith("__test")

    def test_does_not_touch_production_name(self, monkeypatch):
        """重定向后不得等于生产集合名——这是防线的全部意义。"""
        from core.storage.memory_repo import MemoryRepository
        from core.storage.memory_repo_v2 import MemoryRepositoryV2

        install_test_milvus_collection(monkeypatch)
        assert MemoryRepository.COLLECTION != "long_term_memory"
        assert MemoryRepositoryV2.COLLECTION != "long_term_memory_v2"

    def test_is_idempotent(self, monkeypatch):
        """重复调用不叠加后缀（autouse 防线与 fake_storage 都会调）。"""
        from core.storage.memory_repo_v2 import MemoryRepositoryV2

        install_test_milvus_collection(monkeypatch)
        once = MemoryRepositoryV2.COLLECTION
        install_test_milvus_collection(monkeypatch)
        assert MemoryRepositoryV2.COLLECTION == once
        assert once.endswith("__test") and not once.endswith("__test__test")


class TestAutouseWiring:
    """根 `conftest.py::_guard_real_database` 的**接线**判据。

    这一组补的是最隐蔽的一类假绿：「装置写对了，但没装在改动上」。把 autouse
    fixture 摘掉 / 改错判据 / 拼错 fixture 名，上面那些直接调装置的用例**照样全绿**，
    而防线已经不再保护任何测试——本文件此前声称由 `test_real_db_guard_e2e.py`
    覆盖的正是这件事，但那个文件并不存在。

    本类**被 autouse 防线覆盖**（marker=unit ⇒ 非豁免层）因此它断言的是自己的
    运行环境，而不是某个显式安装的副本。
    """

    def test_mongo_guard_is_installed_by_autouse(self):
        """`DocumentDBFactory.create` 已被换成防线里的包装函数。

        先证接线、再验行为：**顺序是判据的一部分**——接线不在时直接红，
        绝不落到真工厂去构造生产库客户端（见下一个用例）。
        """
        from Module.database.documentdb.registry import DocumentDBFactory

        assert DocumentDBFactory.create.__module__ == _GUARD_MODULE, (
            "autouse 防线没有装到本用例上：`DocumentDBFactory.create` 仍是 "
            f"{DocumentDBFactory.create.__module__} 里的原件——"
            "继续调用会真的去构造生产库客户端，故在此失败而不是往下走"
        )

    def test_production_mongo_redirects_to_in_memory_fake(self):
        """行为判据：生产标识 + 生产连接串 → 内存 fake（不是真后端）。"""
        from Module.database.documentdb.registry import DocumentDBFactory
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

        # 与上一个用例同样的 fail-closed 前置：接线不在就停在这里
        assert DocumentDBFactory.create.__module__ == _GUARD_MODULE

        backend = DocumentDBFactory.create("mongodb/default")
        assert isinstance(backend, FakeDocumentDBBackend), (
            f"生产 Mongo 没有被导流到内存 fake，实际拿到 {type(backend).__name__}"
        )

    def test_milvus_client_is_blocked_for_unit_tests(self):
        """unit/service 层：Milvus 客户端被换成「操作即抛」的桩（它们不需要向量库）。"""
        from Module.database.vectordb.milvus import MilvusClient

        assert MilvusClient.__module__ == _GUARD_MODULE, (
            f"MilvusClient 仍是 {MilvusClient.__module__} 里的真件——"
            "unit/service 层应当被换成操作即抛的桩"
        )

    def test_ltm_collection_is_redirected_for_this_test(self):
        """LTM 集合名在本用例内以 `__test` 结尾（autouse 防线已生效）。"""
        from core.storage.memory_repo import MemoryRepository
        from core.storage.memory_repo_v2 import MemoryRepositoryV2

        assert MemoryRepositoryV2.COLLECTION.endswith("__test")
        assert MemoryRepository.COLLECTION.endswith("__test")
        assert MemoryRepositoryV2.COLLECTION != "long_term_memory_v2"
