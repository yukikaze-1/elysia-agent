"""TTS 模块配置 — 可通过 YAML 文件或环境变量覆盖默认值。"""

from dataclasses import dataclass

from Module.config import ConfigField


@dataclass
class TTSConfig:
    base_url: str = ConfigField(
        default="http://localhost:9880",
        env="GPT_SOVITS_URL",
        help="TTS 服务端点",
    )
    voices_config: str = ConfigField(
        default="",
        help="音色配置 YAML 文件路径，为空则使用内置 voices.yaml",
    )
