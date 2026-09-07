"""Runtime character profile compilation tests."""

import pytest

from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.character.runtime_profile import compile_runtime_profile

pytestmark = pytest.mark.service


def test_compile_custom_revision_uses_only_its_own_authored_fields() -> None:
    definition = CharacterDefinition(
        characterId="chr_custom_a", source="custom", mutable=True
    )
    revision = CharacterRevision(
        characterId="chr_custom_a",
        revision=3,
        displayName="冷静角色",
        promptProfile={
            "corePersonality": "冷静而直接。",
            "relationshipStyle": "尊重距离。",
            "emotionStyle": "克制表达情绪。",
            "speakingStyle": "短句，不使用口癖。",
            "conversationInitiative": "少追问。",
            "examples": [{"user": "你好", "assistant": "你好。"}],
            "ignored": "must not leak",
        },
        runtimeDefaults={
            "voice": {"voiceId": "voice-a"},
            "emotion": {"idleExpressions": ["neutral", 42]},
            "wakeWords": ["小冷", None],
            "proactive": {
                "enabled": True,
                "intervalSeconds": 120,
                "triggers": ["idle", {"type": "calendar"}, {"bad": "x"}],
            },
        },
    )

    profile = compile_runtime_profile(definition, revision)

    assert profile.config.name == "冷静角色"
    assert profile.config.base_personality == "冷静而直接。"
    assert profile.config.speaking_style == "短句，不使用口癖。"
    assert profile.config.voice == "voice-a"
    assert profile.config.emotion.idle_expressions == ["neutral"]
    assert profile.config.wake_words == ["小冷"]
    assert profile.config.proactive.enabled is True
    assert profile.config.proactive.interval == 120
    assert profile.config.proactive.triggers == [
        {"type": "idle"}, {"type": "calendar"}
    ]
    assert profile.relationship_style == "尊重距离。"
    assert profile.emotion_style == "克制表达情绪。"
    assert profile.conversation_initiative == "少追问。"
    assert [(item.user, item.assistant) for item in profile.examples] == [
        ("你好", "你好。")
    ]
    assert profile.builtin_template_role is None


def test_compile_builtin_records_trusted_template_role() -> None:
    definition = CharacterDefinition(
        characterId="chr_builtin_cyrene", slug="cyrene", source="builtin"
    )
    revision = CharacterRevision(
        characterId=definition.characterId,
        displayName="Cyrene",
    )

    profile = compile_runtime_profile(definition, revision)

    assert profile.builtin_template_role == "cyrene"
