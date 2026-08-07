"""Module.tool — 可复用工具框架（零业务依赖）。"""

from .protocol import (
    ErrorType,
    MediaItem,
    PluginContext,
    ToolContext,
    ToolDefinition,
    ToolEmitter,
    ToolExecutor,
    ToolParameter,
    ToolPlugin,
    ToolProgress,
    ToolResult,
    ToolResultType,
    ToolStatus,
)
from .capability import Capability, RBACLevel
from .safety import SafetyProfile, SafetyPipeline
from .manager import ToolManager
from .tool_descriptor import Tool

__all__ = [
    # 基类
    "ToolPlugin",
    "ToolExecutor",
    # 管理
    "ToolManager",
    # 数据模型
    "ErrorType",
    "Capability",
    "RBACLevel",
    "Tool",
    "MediaItem",
    "PluginContext",
    "ToolResultType",
    "ToolStatus",
    "ToolContext",
    "ToolDefinition",
    "ToolEmitter",
    "ToolParameter",
    "ToolProgress",
    "ToolResult",
    # 安全
    "SafetyProfile",
    "SafetyPipeline",
]
