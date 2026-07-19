"""适配器层 — 封装 Module/ 后端，为 core/ 层提供统一接口。

LLM: LLMAdapter + tool_call 解析（重试、热切换、streaming）
TTS: TTSStreamManager（句队列、emoji 同步、门控播放）
"""

from .llm_adapter import LLMAdapter, ThoughtResult
from .tts_adapter import TTSStreamManager, is_emoji_only

__all__ = [
    "LLMAdapter",
    "ThoughtResult",
    "TTSStreamManager",
    "is_emoji_only",
]
