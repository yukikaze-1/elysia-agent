"""VectorDBConfig 单元测试 — 默认值、env 覆盖、secret 字段、from_config()。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from Module._common.config_test_helpers import make_config_test_classes
from Module.database.vectordb.config import VectorDBConfig
from Module.database.vectordb.registry import VectorDBFactory

# 替换原来的 TestVectorDBConfigDefaults（工厂生成，取 [0]）
TestVectorDBConfigDefaults = pytest.mark.unit(
    make_config_test_classes(
        VectorDBConfig,
        defaults={"base_url": "http://localhost:19530"},
        overrides={"base_url": "http://test:19530"},
        env_key="MILVUS_URI",
        yaml_key="VectorDB",
    )[0]
)

# 保留原始 TestVectorDBFromConfig（含 Factory 集成测试）
@pytest.mark.unit
class TestVectorDBFromConfig:
    @patch("Module.database.vectordb.milvus.MilvusClient", autospec=True)
    def test_from_config_passes_params(self, mock_client, monkeypatch):
        monkeypatch.delenv("MILVUS_TOKEN", raising=False)
        cfg = VectorDBConfig(base_url="http://test:19530", token="tk")
        backend = VectorDBFactory.from_config("milvus/testdb", cfg)
        assert backend._name == "testdb"
        assert mock_client.call_args.kwargs["uri"] == "http://test:19530"
        assert mock_client.call_args.kwargs["token"] == "tk"

    @patch("Module.database.vectordb.milvus.MilvusClient", autospec=True)
    def test_overrides_take_priority(self, mock_client, monkeypatch):
        monkeypatch.delenv("MILVUS_TOKEN", raising=False)
        monkeypatch.setenv("MILVUS_URI", "http://localhost:19530")
        cfg = VectorDBConfig(token="config-token")
        backend = VectorDBFactory.from_config(
            "milvus/db", cfg, token="override-token"
        )
        assert backend._name == "db"
        assert mock_client.call_args.kwargs["token"] == "override-token"
