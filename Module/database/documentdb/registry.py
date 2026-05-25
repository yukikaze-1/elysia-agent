import dataclasses
import os
from typing import TYPE_CHECKING

from Module.logger import get_caller_logger
from .mongodb import MongoDBBackend

if TYPE_CHECKING:
    from .config import DocumentDBConfig
    from .protocol import DocumentDBBackend

_BACKENDS: dict[str, dict] = {
    "mongodb": {
        "base_url": "mongodb://localhost:27017",
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
        base_url = kwargs.pop("base_url", "") or os.getenv(cfg["env_key"], "") or cfg["base_url"]

        if "logger" not in kwargs:
            kwargs["logger"] = get_caller_logger()

        return MongoDBBackend(uri=base_url, database=database, **kwargs)

    @classmethod
    def from_config(cls, provider_id: str, config: "DocumentDBConfig", **overrides) -> "DocumentDBBackend":
        """从 DocumentDBConfig dataclass 创建后端，overrides 优先级最高。"""
        kwargs = dataclasses.asdict(config)
        kwargs.update(overrides)
        return cls.create(provider_id, **kwargs)
