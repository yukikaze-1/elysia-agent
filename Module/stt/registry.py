"""STT 后端注册表 + 工厂。按 provider/model 标识创建后端实例。"""

import dataclasses
from typing import TYPE_CHECKING

from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from .sensevoice import SenseVoiceBackend

if TYPE_CHECKING:
    from .config import STTConfig
    from .protocol import STTBackend

# 注意：TTS/STT 使用 {MODULE}_API_URL 环境变量命名格式，
# LLM 使用 {MODULE}_API_BASE 格式。后续应统一。
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
    "custom": {
        "base_url": "",
        "env_key": "CUSTOM_STT_API_KEY",
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
        base_url = resolve_base_url("", cfg, kwargs.pop("base_url", None))

        env_key = cfg["env_key"]
        api_key = resolve_api_key(env_key, cfg, kwargs.pop("api_key", None))

        # custom provider — 使用 OpenAI 兼容 STT 后端
        if provider == "custom":
            from .openai_stt import OpenAICompatSTTBackend
            if not base_url:
                raise ValueError("custom STT provider requires base_url")
            if "logger" not in kwargs:
                kwargs["logger"] = inject_logger(__name__)
            return OpenAICompatSTTBackend(model=model, api_key=api_key, base_url=base_url, **kwargs)

        if "logger" not in kwargs:
            kwargs["logger"] = inject_logger(__name__)

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
        # model/api_key 由 registry.create() 处理，不通过 config 传递
        kwargs.pop("model", None)
        kwargs.pop("api_key", None)
        kwargs.update(overrides)
        return cls.create(model_id, **kwargs)
