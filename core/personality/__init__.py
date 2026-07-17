"""性格引擎 — 四层性格模型 (Core→Self→State→Mood) + 情绪引擎。"""
from .engine import CharacterEngine
from .reflector import CharacterReflector

__all__ = ["CharacterEngine", "CharacterReflector"]
