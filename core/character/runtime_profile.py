"""Compile a persisted character revision into safe runtime inputs.

The wire/storage model intentionally keeps ``promptProfile`` and
``runtimeDefaults`` as JSON objects.  Runtime code must not read those objects
ad hoc: this module performs the single, typed boundary conversion used by the
engine and prompt assembler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.config.character_config import (
    CharacterConfig,
    EmotionConfig,
    ProactiveConfig,
)


@dataclass(frozen=True)
class RuntimeConversationExample:
    """One immutable authored conversation example."""

    user: str
    assistant: str


@dataclass(frozen=True)
class RuntimeCharacterProfile:
    """Typed, immutable inputs consumed by one ``CharacterRuntime``."""

    config: CharacterConfig
    relationship_style: str
    emotion_style: str
    conversation_initiative: str
    examples: tuple[RuntimeConversationExample, ...]
    builtin_template_role: str | None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _text(value: object, *, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _string_list(value: object, *, default: tuple[str, ...] = ()) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    return [item for item in value if isinstance(item, str) and item.strip()]


def _examples(value: object) -> tuple[RuntimeConversationExample, ...]:
    if not isinstance(value, list):
        return ()
    result: list[RuntimeConversationExample] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        user = _text(item.get("user"))
        assistant = _text(item.get("assistant"))
        if user and assistant:
            result.append(RuntimeConversationExample(user=user, assistant=assistant))
    return tuple(result)


def compile_runtime_profile(
    definition: CharacterDefinition,
    revision: CharacterRevision,
) -> RuntimeCharacterProfile:
    """Compile one exact revision without consulting mutable global config.

    ``CharacterService`` already validates authored input on write.  This
    boundary remains defensive for legacy/corrupt documents and supplies only
    neutral runtime defaults; it never falls back to another character.
    """

    prompt = _mapping(revision.promptProfile)
    runtime = _mapping(revision.runtimeDefaults)
    voice = _mapping(runtime.get("voice"))
    emotion = _mapping(runtime.get("emotion"))
    proactive = _mapping(runtime.get("proactive"))

    core_personality = _text(prompt.get("corePersonality"))
    speaking_style = _text(prompt.get("speakingStyle"))
    idle_expressions = _string_list(
        emotion.get("idleExpressions"), default=("neutral",)
    )
    if not idle_expressions:
        idle_expressions = ["neutral"]

    raw_triggers = proactive.get("triggers")
    triggers: list[dict] = []
    if isinstance(raw_triggers, list):
        for trigger in raw_triggers:
            if isinstance(trigger, str) and trigger.strip():
                triggers.append({"type": trigger})
            elif isinstance(trigger, dict):
                trigger_type = trigger.get("type")
                if isinstance(trigger_type, str) and trigger_type.strip():
                    triggers.append({"type": trigger_type})

    interval = proactive.get("intervalSeconds", proactive.get("interval", 300.0))
    if not isinstance(interval, (int, float)) or isinstance(interval, bool):
        interval = 300.0

    config = CharacterConfig(
        name=revision.displayName,
        voice=_text(voice.get("voiceId"), default="default") or "default",
        base_personality=core_personality,
        personality={"speaking_style": speaking_style},
        emotion=EmotionConfig(idle_expressions=idle_expressions),
        wake_words=_string_list(runtime.get("wakeWords")),
        proactive=ProactiveConfig(
            enabled=proactive.get("enabled") is True,
            interval=float(interval),
            triggers=triggers,
        ),
    )
    builtin_role = (
        definition.characterId.removeprefix("chr_builtin_")
        if definition.source == "builtin"
        and definition.characterId.startswith("chr_builtin_")
        else None
    )
    return RuntimeCharacterProfile(
        config=config,
        relationship_style=_text(prompt.get("relationshipStyle")),
        emotion_style=_text(prompt.get("emotionStyle")),
        conversation_initiative=_text(prompt.get("conversationInitiative")),
        examples=_examples(prompt.get("examples")),
        builtin_template_role=builtin_role,
    )
