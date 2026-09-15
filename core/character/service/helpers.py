"""模块级纯函数与共享常量（自 core/character/service.py 按写入链拆出）。

**纯函数**是这里的准入条件：喂入领域模型/标量，返回领域模型或 wire 模型，不碰
repo、连接、运行时。三条写链都要用的常量（分页扫描上界、实例对账重试次数、重置
字段、内置回退角色 ID）也在这里——放这里是为了让读链/写链互不 import。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, cast

from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.character.service.errors import CharacterQueryError, CharacterValidationError
from frontend_contract.rest import (
    CharacterDetail,
    CharacterResetField,
    CharacterRevisionContent,
    CharacterSource,
    CharacterStateImpact,
    CharacterStatePolicy,
    CharacterStatus,
    CharacterSummary,
    CharacterValidationIssue,
    CharacterValidationSeverity,
)

MAX_CHARACTER_LIST_SCAN = 10_000
INSTANCE_RECONCILE_RETRIES = 3
RESET_FIELDS = [
    CharacterResetField.SELF_L2,
    CharacterResetField.STATE_L3,
    CharacterResetField.SPEAKING_STYLE_C6,
    CharacterResetField.REFLECTOR,
    CharacterResetField.MOOD,
]
BUILTIN_FALLBACK_CHARACTER_ID = "chr_builtin_elysia"


def _encode_cursor(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if cursor is None or cursor == "":
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise CharacterQueryError("cursor 无效") from None
    if not isinstance(value, dict):
        raise CharacterQueryError("cursor 无效")
    return value


def _limit(value: int) -> int:
    if not 1 <= value <= 100:
        raise CharacterQueryError("limit 必须在 1 到 100 之间")
    return value


def _mapping(value: object) -> dict[str, Any]:
    """Treat only JSON objects as legacy maps at the domain/wire boundary."""
    return value if isinstance(value, dict) else {}


def _checksum(revision: CharacterRevision) -> str:
    payload = {
        "schemaVersion": revision.schemaVersion,
        "displayName": revision.displayName,
        "description": revision.description,
        "aliases": revision.aliases,
        "promptProfile": revision.promptProfile,
        "runtimeDefaults": revision.runtimeDefaults,
        "presentation": revision.presentation,
        "openingMessage": revision.openingMessage,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _normalise_revision(revision: CharacterRevision) -> CharacterRevision:
    """Fill only known legacy omissions; never copy arbitrary keys to wire."""
    raw_prompt = _mapping(revision.promptProfile)
    prompt = {
        key: raw_prompt[key]
        for key in (
            "corePersonality", "relationshipStyle", "emotionStyle",
            "speakingStyle", "conversationInitiative", "examples",
        )
        if key in raw_prompt
    }
    prompt.setdefault("corePersonality", "暂无核心人格设定。")
    prompt.setdefault("relationshipStyle", "保持自然、尊重的关系边界。")
    prompt.setdefault("emotionStyle", "根据上下文自然表达情绪。")
    prompt.setdefault("speakingStyle", "用清晰、自然的方式交流。")
    prompt.setdefault("conversationInitiative", "在合适时主动回应用户。")
    prompt.setdefault("examples", [])

    runtime = _mapping(revision.runtimeDefaults)
    voice_raw = _mapping(runtime.get("voice"))
    voice = {
        "providerId": voice_raw.get("providerId"),
        "voiceId": voice_raw.get("voiceId"),
    }
    emotion_raw = _mapping(runtime.get("emotion"))
    emotion = {
        "idleExpressions": list(emotion_raw.get("idleExpressions") or []),
    }
    proactive_raw = _mapping(runtime.get("proactive"))
    proactive = {
        "enabled": bool(proactive_raw.get("enabled", False)),
        "intervalSeconds": proactive_raw.get(
            "intervalSeconds", proactive_raw.get("interval", 1800)
        ),
    }
    trigger_values = proactive_raw.get("triggers") or []
    # The legacy builtin YAML used structured trigger objects.  The frozen wire
    # intentionally exposes only stable string trigger IDs, never arbitrary dicts.
    proactive["triggers"] = [
        value if isinstance(value, str) else str(value.get("type", ""))
        for value in trigger_values
        if isinstance(value, str) or isinstance(value, dict)
    ]
    runtime = {
        "voice": voice,
        "emotion": emotion,
        "wakeWords": list(runtime.get("wakeWords") or []),
        "proactive": proactive,
    }
    presentation_raw = _mapping(revision.presentation)
    presentation = cast(dict[str, object], {
        "avatarAssetId": presentation_raw.get("avatarAssetId"),
        "live2dModelId": presentation_raw.get("live2dModelId"),
        "themeColor": presentation_raw.get("themeColor"),
    })

    result = revision.model_copy(deep=True)
    result.promptProfile = prompt
    result.runtimeDefaults = runtime
    result.presentation = presentation
    if not result.checksum:
        result.checksum = _checksum(result)
    return result


def _revision_wire(revision: CharacterRevision) -> CharacterRevisionContent:
    """Construct the strict public revision model (the dict boundary)."""
    normalised = _normalise_revision(revision)
    return CharacterRevisionContent.model_validate(
        normalised.model_dump(mode="json", exclude={"statePolicy"})
    )


def _summary(defn: CharacterDefinition, revision: CharacterRevision) -> CharacterSummary:
    current = _revision_wire(revision)
    return CharacterSummary(
        characterId=defn.characterId,
        source=CharacterSource(defn.source),
        mutable=defn.mutable,
        status=CharacterStatus(defn.status),
        currentRevision=defn.currentRevision,
        displayName=current.displayName,
        description=current.description,
        aliases=current.aliases,
        presentation=current.presentation,
        updatedAt=defn.updatedAt,
    )


def _detail(defn: CharacterDefinition, revision: CharacterRevision) -> CharacterDetail:
    current = _revision_wire(revision)
    return CharacterDetail(
        summary=CharacterSummary(
            characterId=defn.characterId,
            source=CharacterSource(defn.source),
            mutable=defn.mutable,
            status=CharacterStatus(defn.status),
            currentRevision=defn.currentRevision,
            displayName=current.displayName,
            description=current.description,
            aliases=current.aliases,
            presentation=current.presentation,
            updatedAt=defn.updatedAt,
        ),
        current=current,
    )


def _parse_state_policy(policy: str) -> CharacterStatePolicy:
    try:
        return CharacterStatePolicy(policy)
    except ValueError:
        raise CharacterValidationError("statePolicy 无效") from None


def _state_impact(
    policy: str,
    *,
    affected_instances: int = 0,
    pending_instances: int = 0,
) -> CharacterStateImpact:
    parsed = _parse_state_policy(policy)
    fields = RESET_FIELDS if parsed is CharacterStatePolicy.RESET_EVOLUTION else []
    return CharacterStateImpact(
        statePolicy=parsed,
        affectedInstances=affected_instances,
        pendingInstances=pending_instances,
        resetFields=fields,
    )


def _semantic_issues(text: str, path: str) -> list[CharacterValidationIssue]:
    from core.character.draft import validate_semantic

    return [
        CharacterValidationIssue(
            severity=CharacterValidationSeverity(issue.severity),
            code=issue.code,
            path=path,
            message=issue.message,
        )
        for issue in validate_semantic(text)
    ]
