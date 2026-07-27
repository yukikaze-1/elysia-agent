"""core/config/ — 应用配置和角色配置。"""
from .app_config import AppConfig, LLMConfig, MemoryConfig, ServerConfig, TTSConfig
from .character_config import CharacterConfig, EmotionConfig, ProactiveConfig

__all__ = [
    "AppConfig", "LLMConfig", "MemoryConfig", "ServerConfig", "TTSConfig",
    "CharacterConfig", "EmotionConfig", "ProactiveConfig",
]
