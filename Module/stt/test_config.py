"""STTConfig 单元测试 — 默认值、env 覆盖、from_config()。"""

from __future__ import annotations

from Module.config import ConfigLoader
from Module.stt.config import STTConfig
from Module.stt.registry import STTFactory


class TestSTTConfigDefaults:
    def test_defaults(self):
        cfg = ConfigLoader(STTConfig).load()
        assert cfg.base_url == "http://localhost:20042"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("SENSEVOICE_API_URL", "http://192.168.1.18:20042")
        cfg = ConfigLoader(STTConfig).load()
        assert cfg.base_url == "http://192.168.1.18:20042"


class TestSTTFromConfig:
    def test_from_config_passes_params(self):
        cfg = STTConfig(base_url="http://test:20042")
        backend = STTFactory.from_config("sensevoice/sense-voice", cfg)
        assert backend._model == "sense-voice"
        assert str(backend._client.base_url).rstrip("/") == "http://test:20042"

    def test_overrides_take_priority(self):
        cfg = STTConfig(base_url="http://default:20042")
        backend = STTFactory.from_config(
            "sensevoice/custom", cfg, base_url="http://overridden:20042"
        )
        assert backend._model == "custom"
        assert str(backend._client.base_url).rstrip("/") == "http://overridden:20042"
