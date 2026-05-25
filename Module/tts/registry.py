"""TTS 后端注册表 + 工厂。按 provider/backend-name 标识创建后端实例。"""

import dataclasses
import os
from typing import TYPE_CHECKING

from Module.logger import get_caller_logger
from .gpt_sovits import GPTSoVitsBackend

if TYPE_CHECKING:
    from .config import TTSConfig
    from .protocol import TTSBackend

_BACKENDS: dict[str, dict] = {
    "gpt-sovits": {
        "base_url": "http://localhost:9880",
        "env_key": "GPT_SOVITS_URL",
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

        if "logger" not in kwargs:
            kwargs["logger"] = get_caller_logger()

        return GPTSoVitsBackend(base_url=base_url, **kwargs)

    @classmethod
    def from_config(cls, provider_id: str, config: "TTSConfig", **overrides) -> "TTSBackend":
        """从 TTSConfig dataclass 创建后端，overrides 优先级最高。"""
        kwargs = dataclasses.asdict(config)
        kwargs.update(overrides)
        return cls.create(provider_id, **kwargs)
