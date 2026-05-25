"""STT 后端注册表 + 工厂。按 provider/model 标识创建后端实例。"""

import dataclasses
import os
from typing import TYPE_CHECKING

from Module.logger import get_caller_logger
from .sensevoice import SenseVoiceBackend

if TYPE_CHECKING:
    from .config import STTConfig
    from .protocol import STTBackend

_BACKENDS: dict[str, dict] = {
    "sensevoice": {
        "base_url": "http://localhost:20042",
        "env_key": "",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
    },
    "deepgram": {
        "base_url": "https://api.deepgram.com/v1",
        "env_key": "DEEPGRAM_API_KEY",
    },
}


class STTFactory:
    """静态工厂 — STTFactory.create("sensevoice/sense-voice")。"""

    @staticmethod
    def create(model_id: str, **kwargs) -> "STTBackend":
        if "/" not in model_id:
            raise ValueError(
                f"Invalid model_id '{model_id}': expected 'provider/model-name' format"
            )
        provider, model = model_id.split("/", 1)

        if provider not in _BACKENDS:
            raise ValueError(
                f"Unknown provider '{provider}'. Known: {', '.join(sorted(_BACKENDS))}"
            )

        if provider in ("openai", "deepgram"):
            raise NotImplementedError(f"{provider} backend is not yet implemented")

        cfg = _BACKENDS[provider]
        base_url = kwargs.pop("base_url", cfg["base_url"])

        env_key = cfg["env_key"]
        api_key = kwargs.pop("api_key", "") or os.getenv(env_key, "")

        if "logger" not in kwargs:
            kwargs["logger"] = get_caller_logger()

        return SenseVoiceBackend(
            model=model,
            api_key=api_key,
            base_url=base_url,
            **kwargs,
        )

    @classmethod
    def from_config(cls, model_id: str, config: "STTConfig", **overrides) -> "STTBackend":
        """从 STTConfig dataclass 创建后端，overrides 优先级最高。"""
        kwargs = dataclasses.asdict(config)
        kwargs.update(overrides)
        return cls.create(model_id, **kwargs)
