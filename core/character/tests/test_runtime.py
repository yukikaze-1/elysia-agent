# core/character/tests/test_runtime.py
import pytest
pytestmark = pytest.mark.unit   # 纯容器
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity


def _make_runtime():
    instance = CharacterInstance(instanceId="chi_test", characterId="chr_test")
    definition = CharacterDefinition(characterId="chr_test")
    revision = CharacterRevision(characterId="chr_test", displayName="Test")
    ui = UserIdentity()
    return CharacterRuntime(instance, definition, revision, ui)


def test_runtime_holds_snapshot():
    rt = _make_runtime()
    assert rt.instance.instanceId == "chi_test"
    assert rt.definition.characterId == "chr_test"
    assert rt.revision.displayName == "Test"
    assert rt.user_identity.pronouns == "她"


def test_runtime_lazy_engine():
    rt = _make_runtime()
    assert rt.engine is None   # 懒建


"""CharacterRuntime 激活测试（M3：engine per-instance + state 真实化）。"""
import pytest
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId

def _inst(state=None):
    return CharacterInstance(instanceId="chi_x", characterId="chr_x", state=state)

def _rt(state=None):
    return CharacterRuntime(
        instance=_inst(state), definition=CharacterDefinition(characterId="chr_x"),
        revision=CharacterRevision(characterId="chr_x", revision=1, displayName="X"),
        user_identity=UserIdentity(userId="usr_local_default"),
    )

def test_initial_state_empty_when_none():
    rt = _rt()
    assert rt.state is not None
    assert rt.state.instance_id == "chi_x"

def test_initial_state_loaded_when_present():
    rt = _rt(state=CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s"), self_l2="abc"))
    assert rt.state.self_l2 == "abc"

def test_engine_lazy_bind():
    rt = _rt()
    assert rt.engine is None
    rt.ensure_engine(factory=lambda: object())
    assert rt.engine is not None

def test_snapshot_state_returns_copy():
    rt = _rt(state=CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s")))
    snap = rt.snapshot_state()
    snap.self_l2 = "mutated"
    assert rt.state.self_l2 == ""

@pytest.mark.asyncio
async def test_flush_persists_state():
    calls = []
    rt = _rt()
    rt.state.self_l2 = "v2"
    async def _persist(state):
        calls.append(state.self_l2)
    rt.set_persist_fn(_persist)
    await rt.flush()
    assert calls == ["v2"]


def test_runtime_holds_memory_components():
    rt = CharacterRuntime(
        instance=CharacterInstance(instanceId="chi_a", characterId="chr_a"),
        definition=CharacterDefinition(characterId="chr_a"),
        revision=CharacterRevision(characterId="chr_a", revision=1, displayName="A"),
        user_identity=UserIdentity(userId="usr_a", displayName="you"),
    )
    assert rt.memory_view is None
    assert rt.consolidator is None
    assert rt.reflector is None
