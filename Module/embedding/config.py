"""Embedding 模块配置 — 可通过 YAML 文件或环境变量覆盖默认值。"""

from dataclasses import dataclass

from Module.config import ConfigField


@dataclass
class EmbeddingConfig:
    base_url: str = ConfigField(
        default="https://api.openai.com/v1",
        env="OPENAI_API_BASE",
        help="OpenAI 兼容 embedding API 端点",
    )
    api_key: str = ConfigField(
        default="",
        env="OPENAI_API_KEY",
        secret=True,
        help="API 密钥",
    )
