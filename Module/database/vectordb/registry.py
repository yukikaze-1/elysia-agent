import dataclasses
import os
from typing import TYPE_CHECKING

from Module.logger import get_caller_logger
from .milvus import MilvusBackend

if TYPE_CHECKING:
    from .config import VectorDBConfig
    from .protocol import VectorDBBackend

_BACKENDS: dict[str, dict] = {
    "milvus": {
        "base_url": "http://localhost:19530",
        "env_key": "MILVUS_URI",
    },
    "pinecone": {
        "base_url": "",
        "env_key": "PINECONE_API_KEY",
    },
    "qdrant": {
        "base_url": "http://localhost:6333",
        "env_key": "QDRANT_URL",
    },
}


class VectorDBFactory:
    @staticmethod
    def create(provider_id: str, **kwargs) -> "VectorDBBackend":
        if "/" not in provider_id:
            raise ValueError(
                f"Invalid provider_id '{provider_id}': expected 'provider/name' format"
            )
        provider, name = provider_id.split("/", 1)

        if provider not in _BACKENDS:
            raise ValueError(
                f"Unknown provider '{provider}'. Known: {', '.join(sorted(_BACKENDS))}"
            )

        if provider in ("pinecone", "qdrant"):
            raise NotImplementedError(f"{provider} backend is not yet implemented")

        cfg = _BACKENDS[provider]
        base_url = kwargs.pop("base_url", "") or os.getenv(cfg["env_key"], "") or cfg["base_url"]
        token = kwargs.pop("token", "") or os.getenv("MILVUS_TOKEN", "")

        if "logger" not in kwargs:
            kwargs["logger"] = get_caller_logger()

        return MilvusBackend(uri=base_url, token=token, name=name, **kwargs)

    @classmethod
    def from_config(cls, provider_id: str, config: "VectorDBConfig", **overrides) -> "VectorDBBackend":
        """从 VectorDBConfig dataclass 创建后端，overrides 优先级最高。"""
        kwargs = dataclasses.asdict(config)
        kwargs.update(overrides)
        return cls.create(provider_id, **kwargs)
