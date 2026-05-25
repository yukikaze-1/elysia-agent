"""DocumentDBConfig 单元测试 — 默认值、env 覆盖、from_config()。"""

from __future__ import annotations

from unittest.mock import patch

from Module.config import ConfigLoader
from Module.database.documentdb.config import DocumentDBConfig
from Module.database.documentdb.registry import DocumentDBFactory


class TestDocumentDBConfigDefaults:
    def test_defaults(self):
        cfg = ConfigLoader(DocumentDBConfig).load()
        assert cfg.base_url == "mongodb://localhost:27017"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://remote:27017")
        cfg = ConfigLoader(DocumentDBConfig).load()
        assert cfg.base_url == "mongodb://remote:27017"


class TestDocumentDBFromConfig:
    @patch("Module.database.documentdb.mongodb.AsyncIOMotorClient")
    def test_from_config_passes_params(self, mock_client):
        cfg = DocumentDBConfig(base_url="mongodb://test:27017")
        backend = DocumentDBFactory.from_config("mongodb/testdb", cfg)
        mock_client.assert_called_once_with("mongodb://test:27017")

    @patch("Module.database.documentdb.mongodb.AsyncIOMotorClient")
    def test_overrides_take_priority(self, mock_client):
        cfg = DocumentDBConfig(base_url="mongodb://default:27017")
        backend = DocumentDBFactory.from_config(
            "mongodb/db", cfg, base_url="mongodb://overridden:27017"
        )
        mock_client.assert_called_once_with("mongodb://overridden:27017")
