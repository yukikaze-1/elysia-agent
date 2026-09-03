"""BuiltinCharacterSource — 从现有 character/{name}/config.yaml + personality.md 构建内置角色。

渐进式：不重写 manifest v2，直接复用 CharacterConfig.from_yaml + personality.md。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from paths import get_project_root
from core.config.character_config import CharacterConfig
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.character.instance import CharacterInstance

_BUILTIN_IDS = {
    "elysia": "chr_builtin_elysia",
    "cyrene": "chr_builtin_cyrene",
}


class BuiltinCharacterSource:
    """从 character/<name>/ 构建内置角色的只读来源。"""

    def __init__(self, *, character_dir: Path | None = None):
        self._character_dir = character_dir or (get_project_root() / "character")

    def _config(self, role_name: str) -> CharacterConfig:
        return CharacterConfig.from_yaml(self._character_dir / role_name / "config.yaml")

    def load(self, role_name: str) -> tuple[CharacterDefinition, CharacterRevision, CharacterInstance]:
        cfg = self._config(role_name)
        character_id = self._builtin_id(role_name)
        definition = CharacterDefinition(
            characterId=character_id,
            slug=role_name,
            source="builtin",
            mutable=False,
            status="active",
            currentRevision=1,
        )
        # corePersonality = personality.md 全文；speakingStyle = config.yaml personality.speaking_style
        core_personality = cfg.base_personality
        speaking_style = cfg.speaking_style
        revision = CharacterRevision(
            characterId=character_id,
            revision=1,
            displayName=cfg.name,
            promptProfile={
                "corePersonality": core_personality,
                "speakingStyle": speaking_style,
                # relationshipStyle/emotionStyle/conversationInitiative 本批留空（渲染走模板）
            },
            runtimeDefaults={
                "emotion": {"idleExpressions": cfg.emotion.idle_expressions},
                "wakeWords": cfg.wake_words,
                "proactive": {"enabled": cfg.proactive.enabled,
                              "interval": cfg.proactive.interval,
                              "triggers": cfg.proactive.triggers},
            },
            checksum=hashlib.sha256(core_personality.encode("utf-8")).hexdigest(),
        )
        instance = CharacterInstance(
            instanceId=f"chi_builtin_{role_name}",
            characterId=character_id,
            revision=1,
        )
        return definition, revision, instance

    @staticmethod
    def _builtin_id(role_name: str) -> str:
        return _BUILTIN_IDS.get(role_name, f"chr_builtin_{role_name}")

    def list_names(self) -> list[str]:
        return [d.name for d in self._character_dir.iterdir() if d.is_dir()]
