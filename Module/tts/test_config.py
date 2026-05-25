"""TTSConfig 单元测试 — 默认值、env 覆盖、from_config()。"""

from __future__ import annotations

from Module.config import ConfigLoader
from Module.tts.config import TTSConfig
from Module.tts.registry import TTSFactory


class TestTTSConfigDefaults:
    def test_defaults(self):
        cfg = ConfigLoader(TTSConfig).load()
        assert cfg.base_url == "http://localhost:9880"
        assert cfg.voices_config == ""

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("GPT_SOVITS_URL", "http://192.168.1.100:9880")
        cfg = ConfigLoader(TTSConfig).load()
        assert cfg.base_url == "http://192.168.1.100:9880"

    def test_voices_config_from_yaml(self):
        cfg = ConfigLoader(TTSConfig).load_from_dict({"voices_config": "/path/to/voices.yaml"})
        assert cfg.voices_config == "/path/to/voices.yaml"


class TestTTSFromConfig:
    def test_from_config_passes_params(self):
        cfg = TTSConfig(base_url="http://test:9880")
        backend = TTSFactory.from_config("gpt-sovits/default", cfg)
        # voices_config 为空时回退到内置 gpt_sovits_voices.yaml
        assert backend._voices is not None

    def test_overrides_take_priority(self):
        cfg = TTSConfig(base_url="http://default:9880")
        backend = TTSFactory.from_config(
            "gpt-sovits/elysia", cfg, base_url="http://overridden:9880"
        )
        from Module.tts import GPTSoVitsBackend
        from typing import cast
        backend = cast(GPTSoVitsBackend, backend)
        assert "overridden" in str(backend._client.base_url)
