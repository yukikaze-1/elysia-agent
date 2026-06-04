"""LLM 模块配置 — 可通过 YAML 文件或环境变量覆盖默认值。"""

from dataclasses import dataclass

from Module.config import ConfigField


@dataclass
class LLMConfig:
    model: str = ConfigField(
        default="deepseek/deepseek-chat",
        help="LLM provider/model 标识，格式: provider/model-name",
    )
    base_url: str = ConfigField(
        default="https://api.deepseek.com",
        env="LLM_API_BASE",
        help="LLM API 端点",
    )
    api_key: str = ConfigField(
        default="",
        env="LLM_API_KEY",
        secret=True,
        help="API 密钥",
    )
