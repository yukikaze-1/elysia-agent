"""Module.tool — 可复用工具框架（零业务依赖）。"""

from .protocol import (
    ErrorType,
    MediaItem,
    PluginContext,
    ResultType,
    ToolContext,
    ToolDefinition,
    ToolEmitter,
    ToolExecutor,
    ToolParameter,
    ToolPlugin,
    ToolProgress,
    ToolResult,
)
from .capability import Capability, RBACLevel
from .safety import SafetyProfile, SafetyPipeline
from .manager import ToolManager
from .tool_descriptor import Tool

__all__ = [
    # Base classes
    "ToolPlugin",
    "ToolExecutor",
    # Management
    "ToolManager",
    # Data models
    "ErrorType",
    "Capability",
    "RBACLevel",
    "Tool",
    "MediaItem",
    "PluginContext",
    "ResultType",
    "ToolContext",
    "ToolDefinition",
    "ToolEmitter",
    "ToolParameter",
    "ToolProgress",
    "ToolResult",
    # Safety
    "SafetyProfile",
    "SafetyPipeline",
]
