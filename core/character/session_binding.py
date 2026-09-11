"""新建会话的角色解析、canonical instance 与摘要快照编排。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

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


class SnapshotUnavailableCause(StrEnum):
    """会话角色快照取不到时的**原因分类**（读出期语义区分）。

    背景：`character_profiles=true` 时前端要求每条会话都带角色快照，缺失即整页
    fail-closed。但「取不到快照」有三种截然不同的成因，运维处置方式完全不同——
    此前一律返回 None、无法区分（问题单 §5.2 ★★☆ 读出期区分语义）。

    注意：本枚举是**后端诊断语义**，不参与 wire（响应仍按原样省略 `character` 键，
    契约零变更）；显式下发需前后端同批，另行决策。
    """

    LEGACY_ABSENT = "legacy_absent"
    """该会话本就没有角色绑定（legacy 语义）——**合法**，可继续省略。"""

    INCOMPLETE_BINDING = "incomplete_binding"
    """`character_binding` 字段存在但内容为空/不完整（数据缺口）——需修复或清理。"""

    DANGLING_REFERENCE = "dangling_reference"
    """绑定字段有效，但指向的角色已不可解析（软删/悬空引用，数据缺口）。"""


def canonical_instance_id(owner_user_id: str, character_id: str) -> str:
    """返回同一 (user, character) 稳定且幂等的 canonical instance ID。"""
    if character_id.startswith("chr_builtin_"):
        return f"chi_builtin_{character_id.removeprefix('chr_builtin_')}"
    value = uuid.uuid5(
        _CANONICAL_INSTANCE_NAMESPACE,
        f"{owner_user_id}\x00{character_id}",
    )
    return f"chi_{value.hex}"


def _is_absent_binding(value: object) -> bool:
    """该 binding 值是否表示「本就没有角色绑定」（而非「字段残缺」）。

    `SessionRepository.save()` 对无绑定会话写 `{}`（`normalize_character_binding(...) or {}`），
    故「None / 键缺失 / 空对象 / 全空值对象」都属于「没绑定过」；一旦对象里带了**任何
    非空值**却仍无法归一为合法 binding（如只有 character_id 没有 instance_id），
    则属字段残缺的数据缺口。
    """
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    return not any(v not in (None, "") for v in value.values())


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
        snapshot, _cause = await self.snapshot_with_cause(value)
        return snapshot

    async def snapshot_with_cause(
        self, value: object,
    ) -> tuple[CharacterBindingSnapshot | None, SnapshotUnavailableCause | None]:
        """同 snapshot()，但**区分**取不到快照的成因（读出期语义区分）。

        Returns:
            (快照, None) 成功；取不到时 (None, 原因)。原因见 SnapshotUnavailableCause：
            legacy 无绑定（合法）与两类数据缺口（需处置）由此分开，供调用方分别处置
            （前者静默省略、后者显式告警定位）。
        """
        binding = normalize_character_binding(value)
        if binding is None:
            # 区分「本就没绑定」（None / 空对象 → legacy）与「字段在但内容残缺」（缺口）。
            return None, (
                SnapshotUnavailableCause.LEGACY_ABSENT
                if _is_absent_binding(value)
                else SnapshotUnavailableCause.INCOMPLETE_BINDING
            )
        detail = await self._characters.get(binding["character_id"])
        if detail is None:
            return None, SnapshotUnavailableCause.DANGLING_REFERENCE
        return self._snapshot_from_detail(binding, detail), None

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
