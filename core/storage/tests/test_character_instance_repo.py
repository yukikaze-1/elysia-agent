"""CharacterInstanceRepository 测试（marker=service，内存假后端）。"""
import pytest

from core.storage.character_instance_repo import CharacterInstanceRepository
from core.character.instance import CharacterInstance
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend


def _inst(**over) -> CharacterInstance:
    base = dict(instanceId="chi_x", characterId="chr_x", revision=1, stateVersion=1)
    base.update(over)
    return CharacterInstance(**base)


@pytest.mark.asyncio
async def test_get_save_roundtrip():
    repo = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    inst = _inst(state=CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s")))
    await repo.save(inst)
    got = await repo.get("chi_x")
    assert got is not None and got.instanceId == "chi_x"
    assert got.state.instance_id == "chi_x"


@pytest.mark.asyncio
async def test_cas_state_version_hit():
    repo = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    await repo.save(_inst(stateVersion=1))
    ok = await repo.cas_state_version("chi_x", expected=1,
        new_state=CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s")), new_version=2)
    assert ok is True
    assert (await repo.get("chi_x")).stateVersion == 2


@pytest.mark.asyncio
async def test_cas_state_version_miss_conflict():
    repo = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    await repo.save(_inst(stateVersion=2))
    ok = await repo.cas_state_version("chi_x", expected=1,
        new_state=CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s")), new_version=3)
    assert ok is False


@pytest.mark.asyncio
async def test_offline_get_raises():
    repo = CharacterInstanceRepository(db=None)
    with pytest.raises(Exception):
        await repo.get("chi_x")


@pytest.mark.asyncio
async def test_get_canonical_filters_owner_character_and_flag():
    repo = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    await repo.save(_inst(
        instanceId="chi_a", characterId="chr_a", ownerUserId="usr_a", canonical=True,
    ))
    await repo.save(_inst(
        instanceId="chi_b", characterId="chr_a", ownerUserId="usr_b", canonical=True,
    ))
    got = await repo.get_canonical("usr_a", "chr_a")
    assert got is not None and got.instanceId == "chi_a"
    assert await repo.get_canonical("usr_a", "chr_missing") is None


@pytest.mark.asyncio
async def test_list_by_character_preserves_revision_policy_and_pending_metadata():
    repo = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    await repo.save(_inst(
        instanceId="chi_follow",
        characterId="chr_a",
        revisionPolicy="follow_latest",
        pendingRevision=2,
        pendingStatePolicy="reset_evolution",
    ))
    await repo.save(_inst(
        instanceId="chi_pinned",
        characterId="chr_a",
        revisionPolicy="pinned",
    ))
    await repo.save(_inst(instanceId="chi_other", characterId="chr_b"))

    instances = await repo.list_by_character("chr_a")

    assert [instance.instanceId for instance in instances] == [
        "chi_follow", "chi_pinned",
    ]
    assert instances[0].pendingRevision == 2
    assert instances[0].pendingStatePolicy == "reset_evolution"
    assert instances[1].revisionPolicy == "pinned"


@pytest.mark.asyncio
async def test_revision_transition_cas_updates_state_and_clears_pending():
    repo = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    await repo.save(_inst(
        revision=1,
        stateVersion=4,
        state=CharacterRuntimeState(
            instance_id="chi_x", session_id=SessionId("s"), source_revision=1,
        ),
    ))
    assert await repo.set_pending_revision(
        "chi_x",
        expected_revision=1,
        expected_pending_revision=None,
        pending_revision=2,
        state_policy="preserve",
    )
    candidate = CharacterRuntimeState(
        instance_id="chi_x", session_id=SessionId("s"), source_revision=2,
    )

    ok = await repo.cas_apply_revision(
        "chi_x",
        expected_revision=1,
        expected_state_version=4,
        expected_pending_revision=2,
        new_revision=2,
        new_state=candidate,
        new_version=5,
    )

    assert ok is True
    updated = await repo.get("chi_x")
    assert updated is not None
    assert updated.revision == 2
    assert updated.stateVersion == 5
    assert updated.state is not None and updated.state.source_revision == 2
    assert updated.pendingRevision is None
    assert updated.pendingStatePolicy is None

    assert not await repo.cas_apply_revision(
        "chi_x",
        expected_revision=1,
        expected_state_version=4,
        expected_pending_revision=2,
        new_revision=2,
        new_state=candidate,
        new_version=5,
    )
