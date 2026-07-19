"""Debug 仪表板 — DebugInfo 聚合 + Prompt/Response 记录器。"""

from .info import DebugInfo, collect, asdict
from .prompt_recorder import PromptRecorder, PromptRecord
from .response_recorder import ResponseRecorder

__all__ = [
    "DebugInfo",
    "collect",
    "asdict",
    "PromptRecorder",
    "PromptRecord",
    "ResponseRecorder",
]
