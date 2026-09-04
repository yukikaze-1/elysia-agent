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
