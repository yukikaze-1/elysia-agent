"""VectorDB 模块配置 — 可通过 YAML 文件或环境变量覆盖默认值。"""

from dataclasses import dataclass

from Module.config import ConfigField


@dataclass
class VectorDBConfig:
    base_url: str = ConfigField(
        default="http://localhost:19530",
        env="MILVUS_URI",
        help="Milvus 服务端点",
    )
    token: str = ConfigField(
        default="",
        env="MILVUS_TOKEN",
        secret=True,
        help="Milvus 认证 token",
    )
