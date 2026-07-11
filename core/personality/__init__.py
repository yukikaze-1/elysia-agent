"""性格引擎 — 四层性格模型 (Core→Self→State→Mood)。"""
from .engine import PersonalityEngine
from .reflector import CharacterReflector

__all__ = ["PersonalityEngine", "CharacterReflector"]
