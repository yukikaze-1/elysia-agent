"""DocumentDBConfig 单元测试 — 默认值、env 覆盖、from_config()。"""

from __future__ import annotations

from unittest.mock import patch

from Module._common.config_test_helpers import make_config_test_classes
from Module.database.documentdb.config import DocumentDBConfig
from Module.database.documentdb.registry import DocumentDBFactory

# 替换原来的 TestDocumentDBConfigDefaults（工厂生成，取 [0]）
TestDocumentDBConfigDefaults = make_config_test_classes(
    DocumentDBConfig,
    defaults={"base_url": "mongodb://localhost:27017"},
    overrides={"base_url": "mongodb://test:27017"},
    env_key="MONGODB_URI",
    yaml_key="DocumentDB",
)[0]

# 保留原始 TestDocumentDBFromConfig（含 Factory 集成测试）
class TestDocumentDBFromConfig:
    @patch("Module.database.documentdb.mongodb.AsyncIOMotorClient")
    def test_from_config_passes_params(self, mock_client):
        cfg = DocumentDBConfig(base_url="mongodb://test:27017")
        backend = DocumentDBFactory.from_config("mongodb/testdb", cfg)
        mock_client.assert_called_once_with("mongodb://test:27017", serverSelectionTimeoutMS=3000)

    @patch("Module.database.documentdb.mongodb.AsyncIOMotorClient")
    def test_overrides_take_priority(self, mock_client):
        cfg = DocumentDBConfig(base_url="mongodb://default:27017")
        backend = DocumentDBFactory.from_config(
            "mongodb/db", cfg, base_url="mongodb://overridden:27017"
        )
        mock_client.assert_called_once_with("mongodb://overridden:27017", serverSelectionTimeoutMS=3000)
