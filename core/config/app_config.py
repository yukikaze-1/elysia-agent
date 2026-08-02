"""App-level configuration dataclass — loaded via Module.config.ConfigLoader."""

from dataclasses import dataclass

from Module.config import ConfigField, Range


@dataclass
class AgentConfig:
    character: str = ConfigField(default="character/elysia/config.yaml")
    user_name: str = ConfigField(default="妖梦")
    max_pending_messages: int = ConfigField(default=32)


@dataclass
class LLMConfig:
    model: str = ConfigField(default="deepseek/deepseek-chat")
    base_url: str = ConfigField(default="https://api.deepseek.com", env="LLM_BASE_URL")
    api_key: str = ConfigField(default="", secret=True, env="LLM_API_KEY")


@dataclass
class MemoryConfig:
    session_max_messages: int = ConfigField(default=20)
    ltm_max_entries: int = ConfigField(default=10000)
    ltm_max_age_days: int = ConfigField(default=90)
    ltm_enable_eviction: bool = ConfigField(default=True)
    ltm_embed_timeout: float = ConfigField(default=10.0)


@dataclass
class ServerConfig:
    host: str = ConfigField(default="0.0.0.0")
    port: int = ConfigField(default=8765, constraints=[Range(1, 65535)])
    auth_token: str = ConfigField(default="", env="SERVER_AUTH_TOKEN")
    image_max_mb: int = ConfigField(default=10)
    image_allowed_types: list[str] = ConfigField(default_factory=lambda: ["image/png", "image/jpeg", "image/gif", "image/webp"])


@dataclass
class TTSConfig:
    enabled: bool = ConfigField(default=False)
    model: str = ConfigField(default="gpt-sovits/default")
    base_url: str = ConfigField(default="http://localhost:9880", env="TTS_API_URL")
    api_key: str = ConfigField(default="", secret=True, env="TTS_API_KEY")
    voices_config: str = ConfigField(default="")
    rate_limit_rpm: int = ConfigField(
        default=20,
        env="TTS_RATE_LIMIT_RPM",
        help="MiniMax 每分钟最大合成请求数（滑动窗口），0 关闭限速",
    )


@dataclass
class STTConfig:
    enabled: bool = ConfigField(default=False)
    model: str = ConfigField(default="sensevoice/sense-voice")
    base_url: str = ConfigField(default="http://localhost:20042", env="STT_API_URL")
    api_key: str = ConfigField(default="", secret=True, env="STT_API_KEY")


@dataclass
class WakeWordConfig:
    enabled: bool = ConfigField(default=False)


@dataclass
class Live2DConfig:
    """Live2D 看板娘配置"""
    static_image: str = ConfigField(default="", env="LIVE2D_STATIC_IMAGE")


@dataclass
class VisionConfig:
    """Vision model config — used by describe_image tool."""
    model: str = ConfigField(default="mimo/mimo-v2.5")


@dataclass
class DatabaseConfig:
    """数据库连接配置 — MongoDB + Milvus。"""
    mongodb_uri: str = ConfigField(
        default="mongodb://localhost:27017",
        env="MONGODB_URI",
        help="MongoDB 连接串（如 mongodb://localhost:27017）",
    )
    milvus_uri: str = ConfigField(
        default="http://localhost:19530",
        env="MILVUS_URI",
        help="Milvus 服务端点（如 http://localhost:19530）",
    )
    milvus_token: str = ConfigField(
        default="",
        secret=True,
        env="MILVUS_TOKEN",
        help="Milvus 认证令牌",
    )
    db_name: str = ConfigField(default="default", help="MongoDB 数据库名")


@dataclass
class AppConfig:
    agent: AgentConfig = ConfigField(default_factory=AgentConfig)
    llm: LLMConfig = ConfigField(default_factory=LLMConfig)
    memory: MemoryConfig = ConfigField(default_factory=MemoryConfig)
    server: ServerConfig = ConfigField(default_factory=ServerConfig)
    tts: TTSConfig = ConfigField(default_factory=TTSConfig)
    stt: STTConfig = ConfigField(default_factory=STTConfig)
    wake_word: WakeWordConfig = ConfigField(default_factory=WakeWordConfig)
    live2d: Live2DConfig = ConfigField(default_factory=Live2DConfig)
    vision: VisionConfig = ConfigField(default_factory=VisionConfig)
    database: DatabaseConfig = ConfigField(default_factory=DatabaseConfig)
