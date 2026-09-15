"""CharacterService 共享基类（自 core/character/service.py 按写入链拆出）。

持有 `__init__` 与三条链共用的管道：连接建立、取定义（含内置目录回退）、草稿二层
校验（`validate` + `_raise_on_validation`）、草稿 → Revision 构造、实例仓储连接、
默认角色回退 ID。

**刻意不协作**：`__init__` 只在这里定义一次，没有任何 `super().__init__(**kwargs)`
链；三个链混入各自继承本基类，装配点按 C3 线性化把它们串起来。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from core.character.catalog import CharacterCatalog
from core.character.definition import CharacterDefinition
from core.character.draft import CharacterDraft
from core.character.revision import CharacterRevision
from core.character.service.errors import (
    CharacterError,
    CharacterStorageUnavailableError,
    CharacterTooLargeError,
    CharacterValidationError,
)
from core.character.service.helpers import (
    BUILTIN_FALLBACK_CHARACTER_ID,
    _checksum,
    _semantic_issues,
)
from core.profile.profile_store import UserIdentityStore
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from frontend_contract.rest import (
    CharacterCreatedBy,
    CharacterValidationIssue,
    CharacterValidationResult,
)

if TYPE_CHECKING:
    from core.character.preview import CharacterPreviewPort
    from core.character.registry import CharacterRuntimeRegistry
    from core.storage.character_instance_repo import CharacterInstanceRepository


class CharacterServiceBase:
    """共享管道基类——三条链的公共 `__init__` 与取定义/校验/草稿转换。"""

    def __init__(
        self,
        *,
        repo: CharacterRepository,
        rev_repo: CharacterRevisionRepository,
        catalog: CharacterCatalog | None,
        user_identity_store: UserIdentityStore,
        instance_repo: "CharacterInstanceRepository | None" = None,
        runtime_registry: "CharacterRuntimeRegistry | None" = None,
        preview_port: "CharacterPreviewPort | None" = None,
    ):
        self.repo = repo
        self.rev_repo = rev_repo
        self.catalog = catalog
        self.user_identity_store = user_identity_store
        self.instance_repo = instance_repo
        self.runtime_registry = runtime_registry
        self.preview_port = preview_port
        self._connected = False
        self._instances_connected = False
        self._builtin_entries: list[
            tuple[CharacterDefinition, CharacterRevision]
        ] | None = None

    async def _ensure_connected(self) -> None:
        if self._connected:
            return
        if not await self.repo.connect():
            raise CharacterStorageUnavailableError("CharacterRepository 连接失败")
        if not await self.rev_repo.connect():
            raise CharacterStorageUnavailableError("CharacterRevisionRepository 连接失败")
        self._connected = True

    async def validate(self, draft: CharacterDraft) -> CharacterValidationResult:
        issues: list[CharacterValidationIssue] = []
        profile = draft.promptProfile
        for field, text in (
            ("corePersonality", profile.corePersonality),
            ("relationshipStyle", profile.relationshipStyle),
            ("speakingStyle", profile.speakingStyle),
            ("emotionStyle", profile.emotionStyle),
            ("conversationInitiative", profile.conversationInitiative),
        ):
            for issue in _semantic_issues(text, f"/promptProfile/{field}"):
                issues.append(issue)
        for index, example in enumerate(profile.examples):
            for field, text in (("user", example.user), ("assistant", example.assistant)):
                for issue in _semantic_issues(
                    text, f"/promptProfile/examples/{index}/{field}"
                ):
                    issues.append(issue)
        estimated = sum(
            len(text)
            for text in (
                profile.corePersonality,
                profile.relationshipStyle,
                profile.emotionStyle,
                profile.speakingStyle,
                profile.conversationInitiative,
            )
        ) + sum(len(example.user) + len(example.assistant) for example in profile.examples)
        return CharacterValidationResult(
            valid=not any(issue.severity == "error" for issue in issues),
            issues=issues,
            estimatedPromptChars=estimated,
        )

    async def _ensure_instance_repo_connected(
        self,
    ) -> "CharacterInstanceRepository | None":
        repo = self.instance_repo
        if repo is None:
            return None
        if not self._instances_connected:
            if not await repo.connect():
                raise CharacterStorageUnavailableError(
                    "CharacterInstanceRepository 连接失败"
                )
            self._instances_connected = True
        return repo

    # ---- internal helpers ----

    def _fallback_character_id(self, archived_id: str) -> str:
        if self.catalog is not None:
            for definition in self.catalog.list():
                if (
                    definition.characterId != archived_id
                    and definition.status == "active"
                    and definition.source == "builtin"
                ):
                    return definition.characterId
        return BUILTIN_FALLBACK_CHARACTER_ID

    def _catalog_pair(
        self, character_id: str
    ) -> tuple[CharacterDefinition, CharacterRevision] | None:
        return (
            self.catalog.get_revision(character_id)
            if self.catalog is not None
            else None
        )

    async def _get_definition(self, character_id: str) -> CharacterDefinition | None:
        definition = await self.repo.get_definition(character_id)
        if definition is not None:
            return definition
        return self.catalog.get(character_id) if self.catalog is not None else None

    async def _get_definition_and_revision(
        self, character_id: str
    ) -> tuple[CharacterDefinition, CharacterRevision] | None:
        definition = await self._get_definition(character_id)
        if definition is None:
            return None
        if definition.source == "builtin":
            pair = self.catalog.get_revision(character_id) if self.catalog is not None else None
            if pair is None:
                raise CharacterStorageUnavailableError("内置角色 revision 缺失")
            return pair
        revision = await self.rev_repo.get_revision(
            character_id, definition.currentRevision
        )
        if revision is None:
            raise CharacterStorageUnavailableError("角色 revision 缺失")
        return definition, revision

    async def _raise_on_validation(
        self, draft: CharacterDraft
    ) -> CharacterError | None:
        result = await self.validate(draft)
        if not result.valid:
            return CharacterValidationError("角色内容校验失败", result.issues)
        size = len(
            json.dumps(
                draft.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
            ).encode()
        )
        if size > 64 * 1024:
            return CharacterTooLargeError("角色内容超过 64KiB")
        return None

    @staticmethod
    def _revision_from_draft(
        character_id: str,
        draft: CharacterDraft,
        *,
        revision: int,
        created_by: CharacterCreatedBy = CharacterCreatedBy.USER,
        state_policy: Literal["preserve", "reset_evolution"] = "preserve",
    ) -> CharacterRevision:
        profile = {
            "corePersonality": draft.promptProfile.corePersonality,
            "relationshipStyle": draft.promptProfile.relationshipStyle,
            "emotionStyle": draft.promptProfile.emotionStyle,
            "speakingStyle": draft.promptProfile.speakingStyle,
            "conversationInitiative": draft.promptProfile.conversationInitiative,
            "examples": [example.model_dump() for example in draft.promptProfile.examples],
        }
        runtime = draft.runtimeDefaults.model_dump(mode="json")
        runtime.setdefault("voice", {"providerId": None, "voiceId": None})
        runtime.setdefault("emotion", {"idleExpressions": []})
        runtime.setdefault("wakeWords", [])
        runtime.setdefault(
            "proactive", {"enabled": False, "intervalSeconds": 1800, "triggers": []}
        )
        presentation = draft.presentation.model_dump(mode="json")
        for key in ("avatarAssetId", "live2dModelId", "themeColor"):
            presentation.setdefault(key, None)
        revision_model = CharacterRevision(
            characterId=character_id,
            revision=revision,
            displayName=draft.displayName,
            description=draft.description,
            aliases=draft.aliases,
            promptProfile=profile,
            runtimeDefaults=runtime,
            presentation=presentation,
            openingMessage=draft.openingMessage,
            statePolicy=state_policy,
            createdBy=created_by,
        )
        revision_model.checksum = _checksum(revision_model)
        return revision_model
