"""真实数据库访问防线的单元测试（marker=unit）。

覆盖 `core/tests/_real_db_guard.py` 的两个可测构件：
- `is_exempt`：marker 豁免判定（integration/slow/external 允许连真实库）
- `make_blocked_client`：替换真实客户端的桩——**构造成功、任何操作即抛并记录**

端到端效果（防线真的拦住写库）由 `core/tests/test_real_db_guard_e2e.py` 以子进程验证，
因为「测试试图连库」在正常会话里会被 autouse 防线直接判失败，无法自测。
"""

from __future__ import annotations

import pytest

from core.tests._real_db_guard import (
    EXEMPT_MARKERS,
    RealDatabaseAccessError,
    install_fake_mongo,
    is_exempt,
    make_blocked_client,
    production_mongo_provider_ids,
)

pytestmark = pytest.mark.unit


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
