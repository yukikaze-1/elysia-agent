"""TTSConfig 单元测试 — 默认值、env 覆盖、from_config()。"""

from __future__ import annotations

from Module._common.config_test_helpers import make_config_test_classes
from Module.tts.config import TTSConfig
from Module.tts.registry import TTSFactory

# 替换原来的 TestTTSConfigDefaults（工厂生成，取 [0]）
TestTTSConfigDefaults = make_config_test_classes(
    TTSConfig,
    defaults={"model": "gpt-sovits/default", "base_url": "http://localhost:9880", "api_key": "", "voices_config": ""},
    overrides={"model": "openai/tts-1", "base_url": "http://test:9880", "api_key": "sk-test", "voices_config": "/tmp/voices.yaml"},
    env_key="TTS_API_URL",
    yaml_key="TTS",
)[0]

# 保留原始 TestTTSFromConfig（含 Factory 集成测试）
class TestTTSFromConfig:
    def test_from_config_passes_params(self):
        from Module.tts.gpt_sovits import GPTSoVitsBackend
        from typing import cast

        cfg = TTSConfig(base_url="http://test:9880")
        backend = cast(GPTSoVitsBackend, TTSFactory.from_config("gpt-sovits/default", cfg))
        # voices_config 为空时回退到内置 gpt_sovits_voices.yaml
        assert backend._voices is not None

    def test_overrides_take_priority(self):
        cfg = TTSConfig(base_url="http://default:9880")
        backend = TTSFactory.from_config(
            "gpt-sovits/elysia", cfg, base_url="http://overridden:9880"
        )
        from Module.tts.gpt_sovits import GPTSoVitsBackend
        from typing import cast
        backend = cast(GPTSoVitsBackend, backend)
        assert "overridden" in backend._base_url
