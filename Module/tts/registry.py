"""TTS 后端注册表 + 工厂。按 provider/backend-name 标识创建后端实例。"""

import dataclasses
import os
from typing import TYPE_CHECKING

from pathlib import Path

import yaml

from Module.logger import get_caller_logger
from .gpt_sovits import GPTSoVitsBackend


def _load_voices(voices_config: str = "") -> dict:
    """加载音色配置，返回 voice_name → config 的 dict。"""
    if voices_config:
        path = Path(voices_config)
    else:
        path = Path(__file__).parent / "gpt_sovits_voices.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


if TYPE_CHECKING:
    from .config import TTSConfig
    from .protocol import TTSBackend

_BACKENDS: dict[str, dict] = {
    "gpt-sovits": {
        "base_url": "http://localhost:9880",
        "env_key": "GPT_SOVITS_URL",
    },
    "custom": {
        "base_url": "",
        "env_key": "CUSTOM_TTS_API_KEY",
    },
}


class TTSFactory:
    """静态工厂 — TTSFactory.create("gpt-sovits/default")。"""

    @staticmethod
    def create(provider_id: str, **kwargs) -> "TTSBackend":
        if "/" not in provider_id:
            raise ValueError(
                f"Invalid provider_id '{provider_id}': expected 'provider/backend-name' format"
            )
        provider, backend_name = provider_id.split("/", 1)

        if provider not in _BACKENDS:
            raise ValueError(
                f"Unknown provider '{provider}'. Known: {', '.join(sorted(_BACKENDS))}"
            )

        cfg = _BACKENDS[provider]
        base_url = kwargs.pop("base_url", "") or os.getenv(cfg["env_key"], "") or cfg["base_url"]

        if provider == "custom" and not base_url:
            raise ValueError("custom TTS provider requires base_url")

        # gpt-sovits: 验证 model（"default" 或已知 voice name，大小写不敏感）
        if provider == "gpt-sovits" and backend_name != "default":
            voices_path = kwargs.get("voices_config", "")
            voices = _load_voices(voices_path)
            voices_lower = {k.lower(): k for k in voices}
            if backend_name.lower() not in voices_lower:
                raise ValueError(
                    f"未知的 TTS model: {backend_name!r}。"
                    f"支持的 model: default, {', '.join(sorted(voices))}"
                )

        # custom provider: 使用 OpenAI 兼容 TTS 协议
        if provider == "custom":
            from .openai_tts import OpenAICompatTTSBackend
            if not base_url:
                raise ValueError("custom TTS provider requires base_url")
            api_key = kwargs.pop("api_key", "") or os.getenv(cfg["env_key"], "")
            if "logger" not in kwargs:
                kwargs["logger"] = get_caller_logger()
            return OpenAICompatTTSBackend(model=backend_name, api_key=api_key, base_url=base_url, **kwargs)

        # gpt-sovits: GPT-SoVits 专有协议
        if "logger" not in kwargs:
            kwargs["logger"] = get_caller_logger()

        return GPTSoVitsBackend(model=backend_name, base_url=base_url, **kwargs)

    @classmethod
    def from_config(cls, provider_id: str, config: "TTSConfig", **overrides) -> "TTSBackend":
        """从 TTSConfig dataclass 创建后端，overrides 优先级最高。"""
        kwargs = dataclasses.asdict(config)
        # model/api_key 由 registry.create() 处理，不通过 config 传递
        kwargs.pop("model", None)
        kwargs.pop("api_key", None)
        kwargs.update(overrides)
        return cls.create(provider_id, **kwargs)
