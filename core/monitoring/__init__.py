"""运行时可观测性：事件总线 + 空闲监控 + Token 追踪。"""
from .event_bus import EventBus, EventPriority, EventType
from .token_tracker import TokenTracker

__all__ = [
    "EventBus",
    "EventPriority",
    "EventType",
    "IdleMonitor",
    "TokenTracker",
]


def __getattr__(name):
    """Lazy import for IdleMonitor — breaks circular import via agent."""
    if name == "IdleMonitor":
        from .idle_monitor import IdleMonitor
        return IdleMonitor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
