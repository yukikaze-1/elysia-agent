"""Character runtime state persistence must remain CAS-safe."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.agent.agent import Agent
from core.character.definition import CharacterDefinition
from core.character.instance import CharacterInstance
from core.character.revision import CharacterRevision
from core.character.runtime import CharacterRuntime
from core.character.service import CharacterStateConflictError
from core.profile.user_identity import UserIdentity
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId
from core.storage.base import StorageUnavailableError

pytestmark = pytest.mark.service


def _runtime() -> CharacterRuntime:
    state = CharacterRuntimeState(
        instance_id="chi_persist",
        session_id=SessionId("session_persist"),
        source_revision=1,
        mood="平静",
    )
    return CharacterRuntime(
        instance=CharacterInstance(
            instanceId="chi_persist",
            characterId="chr_persist",
            revision=1,
            stateVersion=3,
            state=state,
        ),
        definition=CharacterDefinition(
            characterId="chr_persist", source="custom", mutable=True,
        ),
        revision=CharacterRevision(
            characterId="chr_persist", revision=1, displayName="Persist",
        ),
        user_identity=UserIdentity(userId="usr_local_default"),
    )


def _persist_fn(runtime: CharacterRuntime, repo: AsyncMock):
    owner = SimpleNamespace(_instance_repo=repo)
    return Agent._persist_runtime_factory(owner, runtime)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runtime_persist_advances_version_only_after_cas_hit() -> None:
    runtime = _runtime()
    repo = AsyncMock()
    repo.cas_state_version.return_value = True
    candidate = runtime.state.model_copy(deep=True, update={"mood": "开心"})

    await _persist_fn(runtime, repo)(candidate)

    repo.cas_state_version.assert_awaited_once_with(
        "chi_persist", expected=3, new_state=candidate, new_version=4,
    )
    assert runtime.stateVersion == 4
    assert runtime.instance.stateVersion == 4
    assert runtime.instance.state is candidate
    repo.get.assert_not_awaited()
    repo.save_if_absent.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_persist_conflict_never_falls_back_to_overwrite() -> None:
    runtime = _runtime()
    original_state = runtime.instance.state
    repo = AsyncMock()
    repo.cas_state_version.return_value = False
    repo.get.return_value = CharacterInstance(
        instanceId="chi_persist",
        characterId="chr_persist",
        stateVersion=9,
    )

    with pytest.raises(CharacterStateConflictError) as caught:
        await _persist_fn(runtime, repo)(runtime.state)

    assert caught.value.current_state_version == 9
    assert runtime.stateVersion == 3
    assert runtime.instance.stateVersion == 3
    assert runtime.instance.state is original_state
    repo.save.assert_not_awaited()
    repo.save_if_absent.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_persist_creates_only_when_instance_is_absent() -> None:
    runtime = _runtime()
    repo = AsyncMock()
    repo.cas_state_version.return_value = False
    repo.get.return_value = None
    repo.save_if_absent.return_value = True

    await _persist_fn(runtime, repo)(runtime.state)

    inserted = repo.save_if_absent.await_args.args[0]
    assert inserted.instanceId == "chi_persist"
    assert inserted.stateVersion == 4
    assert runtime.stateVersion == 4
    assert runtime.instance.stateVersion == 4
    repo.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_persist_storage_error_does_not_mutate_cas_token() -> None:
    runtime = _runtime()
    repo = AsyncMock()
    repo.cas_state_version.side_effect = StorageUnavailableError("offline")

    with pytest.raises(StorageUnavailableError):
        await _persist_fn(runtime, repo)(runtime.state)

    assert runtime.stateVersion == 3
    assert runtime.instance.stateVersion == 3
    repo.get.assert_not_awaited()
    repo.save_if_absent.assert_not_awaited()
