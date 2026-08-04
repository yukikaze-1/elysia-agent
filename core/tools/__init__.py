"""Elysia 工具插件集合。"""

from .filesystem.plugin import ReadFilePlugin, ListDirPlugin, WriteFilePlugin
from .web.plugin import WebSearchPlugin, WebFetchPlugin
from .memory.plugin import MemorySearchPlugin, MemorySavePlugin
from .time.plugin import TimePlugin
from .reminder.plugin import SetReminderPlugin
from .runtime_config.plugin import RuntimeConfigPlugin
from .self_state.plugin import SelfStatePlugin
from .vision import DescribeImagePlugin
from .desktop.plugin import (
    DesktopFileSelectPlugin,
    DesktopFileReadPlugin,
    DesktopFileWritePlugin,
    DesktopScreenCapturePlugin,
)

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
    DescribeImagePlugin,
    # 批次 4 Desktop 工具（runtime=desktop，delegate 桥；desktop.command.run 不注册）
    DesktopFileSelectPlugin,
    DesktopFileReadPlugin,
    DesktopFileWritePlugin,
    DesktopScreenCapturePlugin,
]

