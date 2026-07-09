"""DocumentDB 模块配置 — 可通过 YAML 文件或环境变量覆盖默认值。"""

from dataclasses import dataclass

from Module.config import ConfigField
from Module.config.providers import load_provider_defaults

_db = load_provider_defaults("database")


@dataclass
class DocumentDBConfig:
    base_url: str = ConfigField(
        default=_db.get("mongodb", {}).get("base_url", "mongodb://localhost:27017"),
        env="MONGODB_URI",
        help="MongoDB 连接串",
    )
