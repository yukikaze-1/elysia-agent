"""CharacterCatalog — 只读内置角色目录（无 Mongo 也能列出/校验 Elysia、Cyrene）。"""

from __future__ import annotations

from core.character.definition import CharacterDefinition
from core.character.builtin_source import BuiltinCharacterSource


class CharacterCatalog:
    """先只读内置角色；custom 留阶段 D。"""

    def __init__(self, *, source: BuiltinCharacterSource):
        self._source = source

    def list(self) -> list[CharacterDefinition]:
        defs = []
        for name in self._source.list_names():
            try:
                definition, _, _ = self._source.load(name)
                defs.append(definition)
            except FileNotFoundError:
                continue
        return defs

    def get(self, character_id: str) -> CharacterDefinition | None:
        for d in self.list():
            if d.characterId == character_id:
                return d
        return None

    def validate(self) -> list[str]:
        """校验所有内置角色，返回失败项（空=全部有效）。"""
        failures = []
        for name in self._source.list_names():
            try:
                self._source.load(name)
            except Exception as e:
                failures.append(f"{name}: {e}")
        return failures
