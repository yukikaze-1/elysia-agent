"""运行时可观测性：事件总线 + 空闲监控 + Token 追踪。"""
from typing import TYPE_CHECKING

from .event_bus import EventBus, EventPriority, EventType
from .token_tracker import TokenTracker

if TYPE_CHECKING:
    # IdleMonitor 仅类型层导入——运行时经 __getattr__ 懒加载（打破经 agent 的循环导入）。
    # TYPE_CHECKING 块让 pyright 感知 __all__ 中的 IdleMonitor（reportUnsupportedDunderAll）。
    from .idle_monitor import IdleMonitor

__all__ = [
    "EventBus",
    "EventPriority",
    "EventType",
    "IdleMonitor",
    "TokenTracker",
]


def __getattr__(name: str):
    """IdleMonitor 懒加载——打破经 agent 的循环导入。"""
    if name == "IdleMonitor":
        from .idle_monitor import IdleMonitor
        return IdleMonitor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
