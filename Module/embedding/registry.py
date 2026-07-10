"""Embedding 后端注册表 + 工厂。按 provider/model 标识创建后端实例。"""

import dataclasses
from typing import TYPE_CHECKING

from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from Module.config.providers import load_provider_defaults
from .huggingface import HuggingFaceBackend
from .openai_compat import OpenAICompatEmbeddingBackend

if TYPE_CHECKING:
    from .config import EmbeddingConfig
    from .protocol import EmbeddingBackend

# provider 默认值统一由 Module/config/providers.yaml 管理
_BACKENDS: dict[str, dict] = load_provider_defaults("embedding")


class EmbeddingFactory:
    """静态工厂 — EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")。"""

    _instances: dict[str, "EmbeddingBackend"] = {}

    @classmethod
    def create(cls, model_id: str, **kwargs) -> "EmbeddingBackend":
        if model_id in cls._instances:
            return cls._instances[model_id]
        if "/" not in model_id:
            raise ValueError(
                f"Invalid model_id '{model_id}': expected 'provider/model-name' format"
            )
        provider, model = model_id.split("/", 1)

        if provider not in _BACKENDS:
            raise ValueError(
                f"Unknown provider '{provider}'. Known: {', '.join(sorted(_BACKENDS))}"
            )

        cfg = _BACKENDS[provider]

        env_key = cfg["env_key"]
        api_key = resolve_api_key(env_key, cfg, kwargs.pop("api_key", None))
        base_url = resolve_base_url(cfg.get("url_key", ""), cfg, kwargs.pop("base_url", None))

        if provider == "huggingface":
            if "logger" not in kwargs:
                kwargs["logger"] = inject_logger(__name__)
            instance = HuggingFaceBackend(model_name=model, **kwargs)
        elif provider == "openai":
            if "logger" not in kwargs:
                kwargs["logger"] = inject_logger(__name__)
            instance = OpenAICompatEmbeddingBackend(model=model, api_key=api_key, base_url=base_url, **kwargs)
        else:
            raise NotImplementedError(f"{provider} backend is not yet implemented")

        cls._instances[model_id] = instance
        return instance

    @classmethod
    async def shutdown(cls) -> None:
        """关闭所有缓存的 embedding 实例，释放显存/内存。幂等。"""
        for name, instance in list(cls._instances.items()):
            try:
                await instance.close()
            except Exception:
                pass
        cls._instances.clear()

    @classmethod
    def from_config(cls, model_id: str, config: "EmbeddingConfig", **overrides) -> "EmbeddingBackend":
        """从 EmbeddingConfig dataclass 创建后端，overrides 优先级最高。"""
        kwargs = dataclasses.asdict(config)
        kwargs.update(overrides)
        return cls.create(model_id, **kwargs)
