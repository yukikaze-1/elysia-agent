"""App-level configuration dataclass — loaded via Module.config.ConfigLoader."""

from dataclasses import dataclass

from Module.config import ConfigField, Range


@dataclass
class AgentConfig:
    character: str = ConfigField(default="character/elysia.yaml")
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
    ltm_embed_timeout: float = ConfigField(default=10.0)


@dataclass
class ServerConfig:
    host: str = ConfigField(default="0.0.0.0")
    port: int = ConfigField(default=8765, constraints=[Range(1, 65535)])


@dataclass
class TTSConfig:
    enabled: bool = ConfigField(default=False)
    model: str = ConfigField(default="gpt-sovits/default")
    base_url: str = ConfigField(default="http://localhost:9880", env="TTS_API_URL")
    api_key: str = ConfigField(default="", secret=True, env="TTS_API_KEY")
    voices_config: str = ConfigField(default="")


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
class AppConfig:
    agent: AgentConfig = ConfigField(default_factory=AgentConfig)
    llm: LLMConfig = ConfigField(default_factory=LLMConfig)
    memory: MemoryConfig = ConfigField(default_factory=MemoryConfig)
    server: ServerConfig = ConfigField(default_factory=ServerConfig)
    tts: TTSConfig = ConfigField(default_factory=TTSConfig)
    stt: STTConfig = ConfigField(default_factory=STTConfig)
    wake_word: WakeWordConfig = ConfigField(default_factory=WakeWordConfig)
