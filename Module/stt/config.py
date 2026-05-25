"""STT 模块配置 — 可通过 YAML 文件或环境变量覆盖默认值。"""

from dataclasses import dataclass

from Module.config import ConfigField


@dataclass
class STTConfig:
    base_url: str = ConfigField(
        default="http://localhost:20042",
        env="SENSEVOICE_API_URL",
        help="STT 服务端点",
    )
