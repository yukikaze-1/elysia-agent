# core/config/tests/test_database_config.py
"""DatabaseConfig 默认值 + 环境变量覆盖测试。"""

import pytest
from Module.config import ConfigLoader
from core.config.app_config import DatabaseConfig

pytestmark = pytest.mark.unit


class TestDatabaseConfigDefaults:
    """默认值验证"""

    def test_mongodb_uri_default(self):
        cfg = DatabaseConfig()
        assert cfg.mongodb_uri == "mongodb://localhost:27017"

    def test_milvus_uri_default(self):
        cfg = DatabaseConfig()
        assert cfg.milvus_uri == "http://localhost:19530"

    def test_milvus_token_default_empty(self):
        cfg = DatabaseConfig()
        assert cfg.milvus_token == ""

    def test_db_name_default(self):
        cfg = DatabaseConfig()
        assert cfg.db_name == "default"


class TestDatabaseConfigEnvOverride:
    """环境变量覆盖——env var 通过 ConfigLoader.load() 解析"""

    def test_mongodb_uri_from_env(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://custom:27017")
        cfg = ConfigLoader(DatabaseConfig).load()
        assert cfg.mongodb_uri == "mongodb://custom:27017"

    def test_milvus_uri_from_env(self, monkeypatch):
        monkeypatch.setenv("MILVUS_URI", "http://milvus:19531")
        cfg = ConfigLoader(DatabaseConfig).load()
        assert cfg.milvus_uri == "http://milvus:19531"
