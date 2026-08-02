import dataclasses
from typing import TYPE_CHECKING

from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from Module.database.documentdb.mongodb import MongoDBBackend

if TYPE_CHECKING:
    from Module.database.documentdb.config import DocumentDBConfig
    from Module.database.documentdb.protocol import DocumentDBBackend

_BACKENDS: dict[str, dict] = {
    "mongodb": {
        "base_url": "",
        "env_key": "MONGODB_URI",
    },
}


class DocumentDBFactory:
    """文档数据库工厂 — 按 provider/database 格式标识创建后端实例。"""

    @staticmethod
    def create(provider_id: str, **kwargs) -> "DocumentDBBackend":
        """创建文档数据库后端实例。

        Args:
            provider_id: 格式为 "provider/database" 的标识字符串，
                如 "mongodb/elysia"。
            **kwargs: 传递给后端构造函数的附加参数（如 logger）。

        Returns:
            对应 provider 的 DocumentDBBackend 实例。

        Raises:
            ValueError: provider_id 格式不合法或 provider 未知时抛出。
        """
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
        """从 DocumentDBConfig dataclass 创建后端。

        Args:
            provider_id: 格式为 "provider/database" 的标识字符串。
            config: DocumentDBConfig dataclass 实例。
            **overrides: 覆盖 config 中字段的附加参数，优先级最高。

        Returns:
            对应 provider 的 DocumentDBBackend 实例。
        """
        kwargs = dataclasses.asdict(config)
        kwargs.update(overrides)
        # 空 base_url 不传入，让 create() 从 env var 解析
        if not kwargs.get("base_url"):
            kwargs.pop("base_url", None)
        return cls.create(provider_id, **kwargs)
