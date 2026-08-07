"""STTConfig 单元测试 — 默认值、env 覆盖、from_config()。"""

from __future__ import annotations

import dataclasses

import pytest

from Module._common.config_test_helpers import make_config_test_classes
from Module.stt.config import STTConfig
from Module.stt.registry import STTFactory

pytestmark = pytest.mark.unit

# 替换原来的 TestSTTConfigDefaults（工厂生成，取 [0]）
TestSTTConfigDefaults = make_config_test_classes(
    STTConfig,
    defaults={"base_url": "http://localhost:20042"},
    overrides={"base_url": "http://test:20042"},
    env_key="SENSEVOICE_URL",
    yaml_key="STT",
)[0]

# 保留原始 TestSTTFromConfig（含 Factory 集成测试）
class TestSTTFromConfig:
    def test_base_url_env_binding_is_sensevoice_url(self):
        """base_url 的 env 绑定必须是 SENSEVOICE_URL（与 providers.yaml url_key / registry 一致）。

        迁移 0a05f497 把 STT URL 规范化到 SENSEVOICE_URL，config 残留 STT_API_URL
        会导致照 config 配置的 env 不被运行时读取（运行时只认 registry 的 url_key）。
        """
        f = next(f for f in dataclasses.fields(STTConfig) if f.name == "base_url")
        assert f.metadata["_config_env"] == "SENSEVOICE_URL"

    def test_from_config_passes_params(self):
        cfg = STTConfig(base_url="http://test:20042")
        backend = STTFactory.from_config("sensevoice/sense-voice", cfg)
        assert backend._model == "sense-voice"  # type: ignore[attr-defined]
        assert str(backend._client.base_url).rstrip("/") == "http://test:20042"  # type: ignore[attr-defined]

    def test_overrides_take_priority(self):
        cfg = STTConfig(base_url="http://default:20042")
        backend = STTFactory.from_config(
            "sensevoice/custom", cfg, base_url="http://overridden:20042"
        )
        assert backend._model == "custom"  # type: ignore[attr-defined]
        assert str(backend._client.base_url).rstrip("/") == "http://overridden:20042"  # type: ignore[attr-defined]
