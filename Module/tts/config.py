"""TTS 模块配置 — 可通过 YAML 文件或环境变量覆盖默认值。"""

from dataclasses import dataclass

from Module.config import ConfigField


@dataclass
class TTSConfig:
    model: str = ConfigField(
        default="gpt-sovits/default",
        help="TTS provider/model 标识，格式: provider/model-name",
    )
    base_url: str = ConfigField(
        default="http://localhost:9880",
        env="TTS_API_URL",
        help="TTS 服务端点",
    )
    api_key: str = ConfigField(
        default="",
        env="TTS_API_KEY",
        secret=True,
        help="API 密钥（自定义端点使用）",
    )
    voices_config: str = ConfigField(
        default="",
        help="音色配置 YAML 文件路径，为空则使用内置 voices.yaml",
    )
    rate_limit_rpm: int = ConfigField(
        default=20,
        env="TTS_RATE_LIMIT_RPM",
        help="MiniMax 每分钟最大合成请求数（滑动窗口），0 关闭限速",
    )
