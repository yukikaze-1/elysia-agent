"""LLMConfig 单元测试 — 默认值、env 覆盖、secret 字段、from_config()。"""

from __future__ import annotations

from Module.config import ConfigLoader
from Module.llm.config import LLMConfig
from Module.llm.registry import LLMFactory


class TestLLMConfigDefaults:
    def test_defaults(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "")
        cfg = ConfigLoader(LLMConfig).load()
        assert cfg.base_url == "https://api.deepseek.com"
        assert cfg.api_key == ""

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("LLM_API_BASE", "https://custom.api/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        cfg = ConfigLoader(LLMConfig).load()
        assert cfg.base_url == "https://custom.api/v1"
        assert cfg.api_key == "sk-test"

    def test_api_key_is_secret(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "")
        cfg = ConfigLoader(LLMConfig).load_from_dict({"api_key": "should-be-ignored"})
        assert cfg.api_key == ""


class TestLLMFromConfig:
    def test_from_config_passes_params(self, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        cfg = LLMConfig(base_url="https://test.api/v1", api_key="sk-xxx")
        backend = LLMFactory.from_config("deepseek/deepseek-v4-pro", cfg)
        assert backend._model == "deepseek-v4-pro"
        assert backend._supports_prefix is True
        assert str(backend._client.base_url).rstrip("/") == "https://test.api/v1"

    def test_overrides_take_priority(self, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        cfg = LLMConfig(base_url="https://default.api/v1")
        backend = LLMFactory.from_config(
            "openai/gpt-5", cfg, base_url="https://overridden.api/v1"
        )
        assert backend._model == "gpt-5"
        assert str(backend._client.base_url).rstrip("/") == "https://overridden.api/v1"
