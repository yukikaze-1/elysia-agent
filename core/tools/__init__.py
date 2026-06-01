"""Elysia 工具插件集合。"""

from .memory import MemorySearchPlugin, MemorySavePlugin
from .time import TimePlugin
from .web import WebSearchPlugin, WebFetchPlugin
from .filesystem import ReadFilePlugin, ListDirPlugin
from .write_file import WriteFilePlugin
from .reminder import SetReminderPlugin
from .runtime_config import RuntimeConfigPlugin
from .self_state import SelfStatePlugin

# 所有插件类（按顺序加载）
ALL_PLUGINS: list[type] = [
    MemorySearchPlugin,
    MemorySavePlugin,
    TimePlugin,
    WebSearchPlugin,
    WebFetchPlugin,
    ReadFilePlugin,
    ListDirPlugin,
    WriteFilePlugin,
    SetReminderPlugin,
    RuntimeConfigPlugin,
    SelfStatePlugin,
]
