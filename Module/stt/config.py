"""STT 模块配置 — 可通过 YAML 文件或环境变量覆盖默认值。"""

from dataclasses import dataclass

from Module.config import ConfigField
from Module.stt.sensevoice import DEFAULT_BASE_URL


@dataclass
class STTConfig:
    model: str = ConfigField(
        default="sensevoice/sense-voice",
        help="STT provider/model 标识，格式: provider/model-name",
    )
    base_url: str = ConfigField(
        default=DEFAULT_BASE_URL,
        env="SENSEVOICE_URL",
        help="STT 服务端点",
    )
    api_key: str = ConfigField(
        default="",
        env="STT_API_KEY",
        secret=True,
        help="API 密钥（自定义端点使用）",
    )
