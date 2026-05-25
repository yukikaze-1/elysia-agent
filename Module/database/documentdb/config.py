"""DocumentDB 模块配置 — 可通过 YAML 文件或环境变量覆盖默认值。"""

from dataclasses import dataclass

from Module.config import ConfigField


@dataclass
class DocumentDBConfig:
    base_url: str = ConfigField(
        default="mongodb://localhost:27017",
        env="MONGODB_URI",
        help="MongoDB 连接串",
    )
