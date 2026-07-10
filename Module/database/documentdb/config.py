"""DocumentDB 模块配置 — 必须通过环境变量或 YAML 设置。"""

from dataclasses import dataclass

from Module.config import ConfigField


@dataclass
class DocumentDBConfig:
    base_url: str = ConfigField(
        default="",
        env="MONGODB_URI",
        help="MongoDB 连接串（必需，如 mongodb://localhost:27017）",
    )
