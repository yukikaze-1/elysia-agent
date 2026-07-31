"""Prompt 模块 — Jinja2 模板渲染 + Brain Prompt 模块化引擎。"""

from .manager import PromptManager
from .protocol import PromptSection, StaticSection, Jinja2Section


__all__ = [
    "PromptManager",
    "PromptSection",
    "StaticSection",
    "Jinja2Section",
]
