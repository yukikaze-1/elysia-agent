"""VectorDB 模块配置 — 必须通过环境变量或 YAML 设置。"""

from dataclasses import dataclass

from Module.config import ConfigField


@dataclass
class VectorDBConfig:
    base_url: str = ConfigField(
        default="",
        env="MILVUS_URI",
        help="Milvus 服务端点（必需，如 http://localhost:19530）",
    )
    token: str = ConfigField(
        default="",
        env="MILVUS_TOKEN",
        secret=True,
        help="Milvus 认证 token",
    )
