"""VectorDBConfig 单元测试 — 默认值、env 覆盖、secret 字段、from_config()。"""

from __future__ import annotations

from unittest.mock import patch

from Module.config import ConfigLoader
from Module.database.vectordb.config import VectorDBConfig
from Module.database.vectordb.registry import VectorDBFactory


class TestVectorDBConfigDefaults:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("MILVUS_URI", raising=False)
        monkeypatch.delenv("MILVUS_TOKEN", raising=False)
        cfg = ConfigLoader(VectorDBConfig).load()
        assert cfg.base_url == "http://localhost:19530"
        assert cfg.token == ""

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MILVUS_URI", "http://remote:19530")
        monkeypatch.setenv("MILVUS_TOKEN", "test-token")
        cfg = ConfigLoader(VectorDBConfig).load()
        assert cfg.base_url == "http://remote:19530"
        assert cfg.token == "test-token"

    def test_token_is_secret(self, monkeypatch):
        monkeypatch.delenv("MILVUS_TOKEN", raising=False)
        cfg = ConfigLoader(VectorDBConfig).load_from_dict({"token": "should-be-ignored"})
        assert cfg.token == ""


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
        cfg = VectorDBConfig(token="config-token")
        backend = VectorDBFactory.from_config(
            "milvus/db", cfg, token="override-token"
        )
        assert backend._name == "db"
        assert mock_client.call_args.kwargs["token"] == "override-token"
