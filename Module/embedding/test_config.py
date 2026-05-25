"""EmbeddingConfig 单元测试 — 默认值、env 覆盖、secret 字段、from_config()。"""

from __future__ import annotations

from Module.config import ConfigLoader
from Module.embedding.config import EmbeddingConfig
from Module.embedding.registry import EmbeddingFactory


class TestEmbeddingConfigDefaults:
    def test_defaults(self):
        cfg = ConfigLoader(EmbeddingConfig).load()
        assert cfg.base_url == "https://api.openai.com/v1"
        assert cfg.api_key == ""

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_BASE", "https://custom.api/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        cfg = ConfigLoader(EmbeddingConfig).load()
        assert cfg.base_url == "https://custom.api/v1"
        assert cfg.api_key == "sk-test"

    def test_api_key_is_secret(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "")
        cfg = ConfigLoader(EmbeddingConfig).load_from_dict({"api_key": "should-be-ignored"})
        assert cfg.api_key == ""


class TestEmbeddingFromConfig:
    def test_from_config_openai(self):
        cfg = EmbeddingConfig(api_key="sk-xxx", base_url="https://custom.api/v1")
        backend = EmbeddingFactory.from_config("openai/text-embedding-3-small", cfg)
        assert backend._model == "text-embedding-3-small"
        assert str(backend._client.base_url).rstrip("/") == "https://custom.api/v1"

    def test_overrides_take_priority(self):
        cfg = EmbeddingConfig(base_url="https://default.api/v1")
        backend = EmbeddingFactory.from_config(
            "openai/text-embedding-3-large", cfg, base_url="https://overridden.api/v1"
        )
        assert backend._model == "text-embedding-3-large"
        assert str(backend._client.base_url).rstrip("/") == "https://overridden.api/v1"
