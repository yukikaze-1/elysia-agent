import dataclasses
import os
from typing import TYPE_CHECKING

from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from .milvus import MilvusBackend

if TYPE_CHECKING:
    from .config import VectorDBConfig
    from .protocol import VectorDBBackend

_BACKENDS: dict[str, dict] = {
    "milvus": {
        "base_url": "",
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
    """向量数据库工厂 — 按 provider/name 格式标识创建后端实例。"""

    @staticmethod
    def create(provider_id: str, **kwargs) -> "VectorDBBackend":
        """创建向量数据库后端实例。

        Args:
            provider_id: 格式为 "provider/name" 的标识字符串，
                如 "milvus/long_term_memory"。
            **kwargs: 传递给后端构造函数的附加参数（如 logger、token）。

        Returns:
            对应 provider 的 VectorDBBackend 实例。

        Raises:
            ValueError: provider_id 格式不合法或 provider 未知时抛出。
            NotImplementedError: provider 尚未实现时抛出。
        """
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
        base_url = resolve_base_url(cfg["env_key"], cfg, kwargs.pop("base_url", None))
        token = kwargs.pop("token", "") or os.getenv("MILVUS_TOKEN", "")

        if not base_url:
            raise ValueError(
                "Milvus 连接串未设置。请在 .env 中设置 MILVUS_URI，"
                "如 MILVUS_URI=http://localhost:19530"
            )

        if "logger" not in kwargs:
            kwargs["logger"] = inject_logger(__name__)

        return MilvusBackend(uri=base_url, token=token, name=name, **kwargs)

    @classmethod
    def from_config(cls, provider_id: str, config: "VectorDBConfig", **overrides) -> "VectorDBBackend":
        """从 VectorDBConfig dataclass 创建后端。

        Args:
            provider_id: 格式为 "provider/name" 的标识字符串。
            config: VectorDBConfig dataclass 实例。
            **overrides: 覆盖 config 中字段的附加参数，优先级最高。

        Returns:
            对应 provider 的 VectorDBBackend 实例。
        """
        kwargs = dataclasses.asdict(config)
        kwargs.update(overrides)
        # 空 base_url 不传入，让 create() 从 env var 解析
        if not kwargs.get("base_url"):
            kwargs.pop("base_url", None)
        return cls.create(provider_id, **kwargs)
