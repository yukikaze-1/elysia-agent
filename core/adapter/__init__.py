"""适配器层 — 封装 Module/ 后端，为 core/ 层提供统一接口。

LLM: LLMAdapter + tool_call 解析（重试、热切换、streaming）
TTS: TTSStreamManager（句队列、emoji 同步、门控播放）
Tool: ToolAdapter（工具子系统生命周期）+ ToolResult / ErrorType / ToolContext 重导出
Message: MessageBuilder（系统 prompt + 历史 → LLM API messages[]）
"""

from .llm_adapter import LLMAdapter, ThoughtResult
from .tts_adapter import TTSStreamManager, is_emoji_only
from .message_builder import MessageBuilder
from .tool_adapter import ToolAdapter

# Re-export ToolResult, ErrorType, ToolContext for consumer convenience
from Module.tool.protocol import ToolResult, ErrorType
from Module.tool import ToolContext

__all__ = [
    "LLMAdapter",
    "ThoughtResult",
    "TTSStreamManager",
    "is_emoji_only",
    "MessageBuilder",
    "ToolAdapter",
    "ToolResult",
    "ErrorType",
    "ToolContext",
]
