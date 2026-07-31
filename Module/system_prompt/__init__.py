"""Prompt 模块 — Jinja2 模板渲染 + Brain Prompt 模块化引擎。"""

import warnings

from .manager import PromptManager
from .protocol import PromptSection, StaticSection, Jinja2Section


def __getattr__(name: str):
    if name == "MessageBuilder":
        # 延迟导入——避免顶层 import Module.system_prompt 连带拉入整个 core.adapter
        # 及其传递依赖（Module.llm/tts/output/tool），违反 Module 层 backend-agnostic 分层
        from core.adapter.message_builder import MessageBuilder
        warnings.warn(
            "Module.system_prompt.MessageBuilder is deprecated. "
            "Use core.adapter.message_builder.MessageBuilder instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return MessageBuilder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "PromptManager",
    "PromptSection",
    "StaticSection",
    "Jinja2Section",
    "MessageBuilder",
]
