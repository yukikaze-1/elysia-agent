"""LLM 后端注册表 + 工厂。按 provider/model 标识创建后端实例。"""

import dataclasses
from typing import TYPE_CHECKING

from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from .openai_compat import OpenAICompatBackend

if TYPE_CHECKING:
    from .config import LLMConfig
    from .protocol import LLMBackend

# 注意：TTS/STT 使用 {MODULE}_API_URL 环境变量命名格式，
# LLM 使用 {MODULE}_API_BASE 格式。后续应统一。
_BACKENDS: dict[str, dict] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "beta_url": "https://api.deepseek.com/beta",
        "env_key": "DEEPSEEK_API_KEY",
        "supports_prefix": True,
        "supports_multimodal": False,
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "supports_prefix": False,
        "supports_multimodal": False,
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "env_key": "",
        "supports_prefix": False,
        "supports_multimodal": False,
    },
    "gemini": {
        # placeholder — not yet implemented
        "base_url": "",
        "env_key": "GEMINI_API_KEY",
        "supports_prefix": False,
        "supports_multimodal": False,
    },
    "mimo": {
        "base_url": "https://api.xiaomimimo.com/v1",
        "env_key": "MIMO_API_KEY",
        "supports_prefix": False,
        "supports_multimodal": True,
    },
    "custom": {
        "base_url": "",
        "env_key": "CUSTOM_LLM_API_KEY",
        "supports_prefix": False,
        "supports_multimodal": False,
    },
}


class LLMFactory:
    """静态工厂 — LLMFactory.create("deepseek/deepseek-v4-pro") 返回配置好的 backend。"""

    @staticmethod
    def create(model_id: str, **kwargs) -> "LLMBackend":
        if "/" not in model_id:
            raise ValueError(
                f"Invalid model_id '{model_id}': expected 'provider/model-name' format"
            )
        provider, model = model_id.split("/", 1)

        if provider not in _BACKENDS:
            raise ValueError(
                f"Unknown provider '{provider}'. Known: {', '.join(sorted(_BACKENDS))}"
            )

        if provider == "gemini":
            raise NotImplementedError("Gemini backend is not yet implemented")

        cfg = _BACKENDS[provider]
        base_url = resolve_base_url("", cfg, kwargs.pop("base_url", None))
        beta_url = kwargs.pop("beta_url", cfg.get("beta_url", ""))

        env_key = cfg["env_key"]
        api_key = resolve_api_key(env_key, cfg, kwargs.pop("api_key", None))

        # custom provider 必须提供 base_url
        if provider == "custom" and not base_url:
            raise ValueError("custom provider requires base_url")

        if "logger" not in kwargs:
            kwargs["logger"] = inject_logger(__name__)

        return OpenAICompatBackend(
            model=model,
            api_key=api_key,
            base_url=base_url,
            beta_url=beta_url,
            supports_prefix=cfg["supports_prefix"],
            **kwargs,
        )

    @classmethod
    def from_config(cls, model_id: str, config: "LLMConfig", **overrides) -> "LLMBackend":
        """从 LLMConfig dataclass 创建后端，overrides 优先级最高。"""
        kwargs = dataclasses.asdict(config)
        # model/api_key 由 registry.create() 处理，不通过 config 传递
        kwargs.pop("model", None)
        kwargs.pop("api_key", None)
        kwargs.update(overrides)
        return cls.create(model_id, **kwargs)

    @classmethod
    def supports_multimodal(cls, model_id: str) -> bool:
        """检测 provider 是否支持多模态（图片输入）。"""
        provider = model_id.split("/")[0] if "/" in model_id else model_id
        return bool(_BACKENDS.get(provider, {}).get("supports_multimodal", False))
