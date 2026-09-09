"""新建会话的角色解析、canonical instance 与摘要快照编排。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from core.character.binding import CharacterBinding, normalize_character_binding
from core.character.instance import CharacterInstance
from core.character.service import (
    BUILTIN_FALLBACK_CHARACTER_ID,
    CharacterArchivedError,
    CharacterNotFoundError,
    CharacterService,
)
from core.profile.profile_store import UserIdentityStore
from core.storage.base import StorageUnavailableError
from core.storage.character_instance_repo import CharacterInstanceRepository
from frontend_contract.rest import CharacterBindingSnapshot, CharacterDetail, CharacterSource


_CANONICAL_INSTANCE_NAMESPACE = uuid.UUID("55a20547-c92d-49ad-93ac-14ac6fe7726b")


def canonical_instance_id(owner_user_id: str, character_id: str) -> str:
    """返回同一 (user, character) 稳定且幂等的 canonical instance ID。"""
    if character_id.startswith("chr_builtin_"):
        return f"chi_builtin_{character_id.removeprefix('chr_builtin_')}"
    value = uuid.uuid5(
        _CANONICAL_INSTANCE_NAMESPACE,
        f"{owner_user_id}\x00{character_id}",
    )
    return f"chi_{value.hex}"


@dataclass(frozen=True)
class ResolvedSessionCharacter:
    binding: CharacterBinding
    snapshot: CharacterBindingSnapshot


class CharacterSessionBindingService:
    """把会话选择的 Definition 解析为用户 canonical instance。"""

    def __init__(
        self,
        *,
        character_service: CharacterService,
        instance_repo: CharacterInstanceRepository,
        user_identity_store: UserIdentityStore,
    ) -> None:
        self._characters = character_service
        self._instances = instance_repo
        self._identity = user_identity_store

    async def resolve_for_create(
        self, character_id: str | None,
    ) -> ResolvedSessionCharacter:
        profile = self._identity.get()
        if profile is None and self._identity.is_authoritative:
            # P0-3：身份不可用时不得静默回退内置默认角色（会让用户的新会话落到
            # 错误角色/身份上下文）——fail-closed，由路由映射为 503。
            raise StorageUnavailableError("用户身份权威存储不可用")
        selected_id = (
            character_id
            or (profile.defaultCharacterId if profile is not None else None)
            or BUILTIN_FALLBACK_CHARACTER_ID
        )
        detail = await self._characters.get(selected_id)
        if detail is None:
            raise CharacterNotFoundError(selected_id)
        if detail.summary.status.value == "archived":
            raise CharacterArchivedError(selected_id)

        owner_user_id = profile.userId if profile is not None else "usr_local_default"
        if detail.summary.source is CharacterSource.BUILTIN:
            instance_id = canonical_instance_id(owner_user_id, selected_id)
        else:
            instance = await self._instances.get_canonical(owner_user_id, selected_id)
            if instance is None:
                instance = CharacterInstance(
                    instanceId=canonical_instance_id(owner_user_id, selected_id),
                    characterId=selected_id,
                    revision=detail.current.revision,
                    ownerUserId=owner_user_id,
                    canonical=True,
                )
                await self._instances.save(instance)
            instance_id = instance.instanceId

        binding = CharacterBinding(
            character_id=selected_id,
            instance_id=instance_id,
        )
        return ResolvedSessionCharacter(
            binding=binding,
            snapshot=self._snapshot_from_detail(binding, detail),
        )

    async def snapshot(self, value: object) -> CharacterBindingSnapshot | None:
        """为 list/detail 的内部 binding 构造当前展示快照；legacy 缺失返回 None。"""
        binding = normalize_character_binding(value)
        if binding is None:
            return None
        detail = await self._characters.get(binding["character_id"])
        if detail is None:
            return None
        return self._snapshot_from_detail(binding, detail)

    @staticmethod
    def _snapshot_from_detail(
        binding: CharacterBinding,
        detail: CharacterDetail,
    ) -> CharacterBindingSnapshot:
        current = detail.current
        return CharacterBindingSnapshot(
            characterId=binding["character_id"],
            characterInstanceId=binding["instance_id"],
            characterRevision=current.revision,
            displayName=current.displayName,
            presentation=current.presentation,
        )
