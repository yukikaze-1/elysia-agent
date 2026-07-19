"""LLMConfig 单元测试 — 默认值、env 覆盖、secret 字段、from_config()。"""

from __future__ import annotations

import pytest

from Module._common.config_test_helpers import make_config_test_classes
from Module.llm.config import LLMConfig
from Module.llm.registry import LLMFactory

pytestmark = pytest.mark.unit

# 替换原来的 TestLLMConfigDefaults（工厂生成，取 [0]）
TestLLMConfigDefaults = make_config_test_classes(
    LLMConfig,
    defaults={"model": "deepseek/deepseek-chat", "base_url": "https://api.deepseek.com", "api_key": ""},
    overrides={"model": "custom/model", "base_url": "http://test:8080", "api_key": "test-key"},
    env_key="LLM_API_BASE",
    yaml_key="LLM",
)[0]

# 保留原始 TestLLMFromConfig（含 Factory 集成测试）
class TestLLMFromConfig:
    def test_from_config_passes_params(self, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        cfg = LLMConfig(base_url="https://test.api/v1", api_key="sk-xxx")
        backend = LLMFactory.from_config("deepseek/deepseek-v4-pro", cfg)
        assert backend._model == "deepseek-v4-pro"  # type: ignore[attr-defined]
        assert backend._supports_prefix is True  # type: ignore[attr-defined]
        assert str(backend._client.base_url).rstrip("/") == "https://test.api/v1"  # type: ignore[attr-defined]

    def test_overrides_take_priority(self, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        cfg = LLMConfig(base_url="https://default.api/v1")
        backend = LLMFactory.from_config(
            "openai/gpt-5", cfg, base_url="https://overridden.api/v1"
        )
        assert backend._model == "gpt-5"  # type: ignore[attr-defined]
        assert str(backend._client.base_url).rstrip("/") == "https://overridden.api/v1"  # type: ignore[attr-defined]
