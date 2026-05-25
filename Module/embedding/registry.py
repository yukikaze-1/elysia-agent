"""Embedding 后端注册表 + 工厂。按 provider/model 标识创建后端实例。"""

import dataclasses
import os
from typing import TYPE_CHECKING

from Module.logger import get_caller_logger
from .huggingface import HuggingFaceBackend
from .openai_compat import OpenAICompatEmbeddingBackend

if TYPE_CHECKING:
    from .config import EmbeddingConfig
    from .protocol import EmbeddingBackend

_BACKENDS: dict[str, dict] = {
    "huggingface": {
        "base_url": "",
        "env_key": "",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
    },
}


class EmbeddingFactory:
    """静态工厂 — EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")。"""

    @staticmethod
    def create(model_id: str, **kwargs) -> "EmbeddingBackend":
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
        api_key = kwargs.pop("api_key", "") or os.getenv(env_key, "")
        base_url = kwargs.pop("base_url", "") or cfg["base_url"]

        if provider == "huggingface":
            if "logger" not in kwargs:
                kwargs["logger"] = get_caller_logger()
            return HuggingFaceBackend(model_name=model, **kwargs)
        elif provider == "openai":
            if "logger" not in kwargs:
                kwargs["logger"] = get_caller_logger()
            return OpenAICompatEmbeddingBackend(model=model, api_key=api_key, base_url=base_url, **kwargs)
        else:
            raise NotImplementedError(f"{provider} backend is not yet implemented")

    @classmethod
    def from_config(cls, model_id: str, config: "EmbeddingConfig", **overrides) -> "EmbeddingBackend":
        """从 EmbeddingConfig dataclass 创建后端，overrides 优先级最高。"""
        kwargs = dataclasses.asdict(config)
        kwargs.update(overrides)
        return cls.create(model_id, **kwargs)
