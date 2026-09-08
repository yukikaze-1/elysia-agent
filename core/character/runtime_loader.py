"""Resolve a persisted character instance to one exact runtime source."""

from __future__ import annotations

from core.character.builtin_source import BuiltinCharacterSource
from core.character.definition import CharacterDefinition
from core.character.instance import CharacterInstance
from core.character.revision import CharacterRevision
from core.character.service import CharacterInstanceNotFoundError, CharacterService
from core.storage.base import StorageUnavailableError
from core.storage.character_instance_repo import CharacterInstanceRepository


class CharacterRuntimeSourceError(RuntimeError):
    """The stored instance and character source do not form a valid runtime."""


class CharacterRuntimeSourceLoader:
    """Load built-in or custom runtime identity without cross-character fallback."""

    def __init__(
        self,
        *,
        characters: CharacterService,
        instances: CharacterInstanceRepository,
        builtins: BuiltinCharacterSource,
        owner_user_id: str,
    ) -> None:
        self._characters = characters
        self._instances = instances
        self._builtins = builtins
        self._owner_user_id = owner_user_id

    async def load(
        self, instance_id: str,
    ) -> tuple[CharacterDefinition, CharacterRevision, CharacterInstance]:
        """Return the exact definition/revision/instance triple for a runtime.

        Built-ins remain usable while Mongo is offline, but custom instances are
        fail-closed.  An invalid custom record is never converted into Elysia (or
        any other role) as a convenience fallback.
        """
        if instance_id.startswith("chi_builtin_"):
            role_name = instance_id.removeprefix("chi_builtin_")
            try:
                definition, revision, seed = self._builtins.load(role_name)
            except (FileNotFoundError, KeyError, ValueError) as exc:
                raise CharacterRuntimeSourceError(
                    f"内置角色运行时不存在: {instance_id}"
                ) from exc
            if seed.instanceId != instance_id:
                raise CharacterRuntimeSourceError(
                    f"内置角色 instance 身份不一致: {instance_id}"
                )
            try:
                persisted = await self._instances.get(instance_id)
            except StorageUnavailableError:
                persisted = None
            if persisted is not None:
                if persisted.characterId != definition.characterId:
                    raise CharacterRuntimeSourceError(
                        f"内置角色 instance/definition 身份不一致: {instance_id}"
                    )
                seed = persisted
            return definition, revision, seed

        try:
            instance = await self._instances.get(instance_id)
        except StorageUnavailableError:
            raise
        if instance is None:
            raise CharacterInstanceNotFoundError(instance_id)
        if instance.ownerUserId != self._owner_user_id:
            # Do not expose whether another user's instance exists.
            raise CharacterInstanceNotFoundError(instance_id)

        # follow_latest is reconciled before resolving the exact revision.  This
        # also recovers a Definition update that committed before its per-instance
        # pending marker could be written.
        if instance.revisionPolicy == "follow_latest":
            instance = await self._characters.reconcile_instance_revision(instance)

        definition, revision = await self._characters.resolve_runtime_source(
            instance.characterId, revision=instance.revision
        )
        if definition.source != "custom":
            raise CharacterRuntimeSourceError(
                f"非内置 instance 不得引用 builtin definition: {instance_id}"
            )
        if definition.characterId != instance.characterId:
            raise CharacterRuntimeSourceError(
                f"角色 instance/definition 身份不一致: {instance_id}"
            )
        return definition, revision, instance
