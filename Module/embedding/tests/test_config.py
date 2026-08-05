"""EmbeddingConfig 单元测试 — 默认值、env 覆盖、secret 字段、from_config()。"""

from __future__ import annotations

import pytest

from Module._common.config_test_helpers import make_config_test_classes
from Module.embedding.config import EmbeddingConfig
from Module.embedding.registry import EmbeddingFactory

# 替换原来的 TestEmbeddingConfigDefaults（工厂生成，取 [0]）
TestEmbeddingConfigDefaults = pytest.mark.unit(
    make_config_test_classes(
        EmbeddingConfig,
        defaults={"base_url": "https://api.openai.com/v1"},
        overrides={"base_url": "http://test:8080/v1"},
        env_key="OPENAI_API_BASE",
        yaml_key="Embedding",
    )[0]
)

# 保留原始 TestEmbeddingFromConfig（含 Factory 集成测试）
@pytest.mark.unit
class TestEmbeddingFromConfig:
    def test_from_config_openai(self):
        cfg = EmbeddingConfig(api_key="sk-xxx", base_url="https://custom.api/v1")
        backend = EmbeddingFactory.from_config("openai/text-embedding-3-small", cfg)
        assert backend._model == "text-embedding-3-small"
        assert str(backend._client.base_url).rstrip("/") == "https://custom.api/v1"

    def test_overrides_take_priority(self):
        # api_key 只用于通过 openai 客户端初始化（迁移后新 openai 强制 credentials），
        # 本测试只验证 base_url override 优先级，key 值无关。
        cfg = EmbeddingConfig(base_url="https://default.api/v1", api_key="sk-test")
        backend = EmbeddingFactory.from_config(
            "openai/text-embedding-3-large", cfg, base_url="https://overridden.api/v1"
        )
        assert backend._model == "text-embedding-3-large"
        assert str(backend._client.base_url).rstrip("/") == "https://overridden.api/v1"
