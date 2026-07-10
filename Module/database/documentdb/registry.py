import dataclasses
from typing import TYPE_CHECKING

from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from .mongodb import MongoDBBackend

if TYPE_CHECKING:
    from .config import DocumentDBConfig
    from .protocol import DocumentDBBackend

_BACKENDS: dict[str, dict] = {
    "mongodb": {
        "base_url": "",
        "env_key": "MONGODB_URI",
    },
}


class DocumentDBFactory:
    @staticmethod
    def create(provider_id: str, **kwargs) -> "DocumentDBBackend":
        if "/" not in provider_id:
            raise ValueError(
                f"Invalid provider_id '{provider_id}': expected 'provider/database' format"
            )
        provider, database = provider_id.split("/", 1)

        if provider not in _BACKENDS:
            raise ValueError(
                f"Unknown provider '{provider}'. Known: {', '.join(sorted(_BACKENDS))}"
            )

        cfg = _BACKENDS[provider]
        base_url = resolve_base_url(cfg["env_key"], cfg, kwargs.pop("base_url", None))

        if not base_url:
            raise ValueError(
                "MongoDB 连接串未设置。请在 .env 中设置 MONGODB_URI，"
                "如 MONGODB_URI=mongodb://localhost:27017"
            )

        if "logger" not in kwargs:
            kwargs["logger"] = inject_logger(__name__)

        return MongoDBBackend(uri=base_url, database=database, **kwargs)

    @classmethod
    def from_config(cls, provider_id: str, config: "DocumentDBConfig", **overrides) -> "DocumentDBBackend":
        """从 DocumentDBConfig dataclass 创建后端，overrides 优先级最高。"""
        kwargs = dataclasses.asdict(config)
        kwargs.update(overrides)
        # 空 base_url 不传入，让 create() 从 env var 解析
        if not kwargs.get("base_url"):
            kwargs.pop("base_url", None)
        return cls.create(provider_id, **kwargs)
