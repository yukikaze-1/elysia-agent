"""Prompt 模块 — Jinja2 模板渲染 + Brain Prompt 模块化引擎。"""

import warnings

from .manager import PromptManager
from .protocol import PromptSection, StaticSection, Jinja2Section

# MessageBuilder 已搬迁到 core/adapter/ — 此处保留 re-export 作为过渡
from core.adapter.message_builder import MessageBuilder as _MessageBuilder


def __getattr__(name: str):
    if name == "MessageBuilder":
        warnings.warn(
            "Module.system_prompt.MessageBuilder is deprecated. "
            "Use core.adapter.message_builder.MessageBuilder instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _MessageBuilder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "PromptManager",
    "PromptSection",
    "StaticSection",
    "Jinja2Section",
    "MessageBuilder",
]
