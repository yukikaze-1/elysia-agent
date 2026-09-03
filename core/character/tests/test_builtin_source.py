"""BuiltinCharacterSource + CharacterCatalog 测试（marker=service）。

从 character/{name}/config.yaml + personality.md 构建，mock 无 DB。
"""
import pytest  # 需先 import pytest 才能用 pytest.mark（plan 文本省略，补上以让测试可收集）
from pathlib import Path

from core.character.builtin_source import BuiltinCharacterSource
from core.character.catalog import CharacterCatalog

pytestmark = pytest.mark.service   # 从 config.yaml 构建，mock 无 DB


def test_builtin_source_load_elysia():
    src = BuiltinCharacterSource(character_dir=Path("character"))
    definition, revision, instance = src.load("elysia")
    assert definition.characterId == "chr_builtin_elysia"
    assert definition.mutable is False
    assert revision.displayName == "Elysia"
    # corePersonality 从 personality.md 加载（Elysia L1 以"爱莉希雅"自称，不含字面"Elysia"——断言非空）
    assert revision.promptProfile["corePersonality"]
    assert len(revision.promptProfile["corePersonality"]) > 100
    # speakingStyle 从 config.yaml personality.speaking_style 加载
    assert revision.promptProfile["speakingStyle"]
    # C1/C2/C3 无源字段本批留空
    assert revision.promptProfile.get("relationshipStyle") in (None, "")


def test_builtin_source_load_cyrene():
    src = BuiltinCharacterSource(character_dir=Path("character"))
    definition, revision, instance = src.load("cyrene")
    assert definition.characterId == "chr_builtin_cyrene"
    assert "Cyrene" in revision.promptProfile["corePersonality"]
    assert revision.promptProfile["speakingStyle"]


def test_catalog_lists_builtins():
    src = BuiltinCharacterSource(character_dir=Path("character"))
    catalog = CharacterCatalog(source=src)
    defs = catalog.list()
    ids = {d.characterId for d in defs}
    assert "chr_builtin_elysia" in ids
    assert "chr_builtin_cyrene" in ids
