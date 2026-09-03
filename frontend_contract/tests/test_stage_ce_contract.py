"""阶段 C+E 多角色 Profile 契约测试（Task 9）。

纯契约模型（仅 frontend_contract，零依赖——不 import core/ Module/），无 I/O，marker=unit。
覆盖：
- ServerInfoFeatures.character_profiles 字段（默认 False）
- UserIdentityProfile 构造 + model_validate 往返
- 其余三个 DTO 的构造与 model_validate 往返
"""
import pytest

from frontend_contract.ws_message import ServerInfoFeatures
from frontend_contract.rest import (
    CharacterInstanceSummary,
    CharacterPublicState,
    CharacterSummary,
    UserIdentityProfile,
)

pytestmark = pytest.mark.unit


def test_server_info_character_profiles_false():
    """特征声明带 character_profiles 字段，且可显式构造为 False。"""
    f = ServerInfoFeatures(character_profiles=False)
    assert f.character_profiles is False
    # 默认值同样为 False（未传时）
    assert ServerInfoFeatures().character_profiles is False
    # 契约层字段不得超前声明为 True（本批恒 false）
    assert f.model_dump()["character_profiles"] is False


def test_user_identity_profile_dto():
    dto = UserIdentityProfile(
        userId="usr_local_default", displayName="妖梦", pronouns="她", initialized=False)
    assert dto.pronouns == "她"
    assert dto.initialized is False
    assert dto.userId == "usr_local_default"
    # 未传的可缺省/带默认字段正确落入默认
    assert dto.preferredAddress is None
    assert dto.revision == 1


def test_user_identity_profile_model_validate_round_trip():
    """model_validate 往返：构造 → wire_dict → 反序列化 → 语义等值。"""
    dto = UserIdentityProfile(
        userId="usr_local_default", displayName="妖梦", pronouns="她", initialized=False)
    rt = UserIdentityProfile.model_validate(dto.model_dump())
    assert rt.model_dump() == dto.model_dump()
    assert rt.pronouns == "她"


def test_character_summary_dto():
    dto = CharacterSummary(characterId="elysia", displayName="Elysia")
    assert dto.characterId == "elysia"
    assert dto.displayName == "Elysia"
    assert dto.source == "builtin"
    assert dto.status == "active"
    rt = CharacterSummary.model_validate(dto.model_dump())
    assert rt.model_dump() == dto.model_dump()


def test_character_instance_summary_dto():
    dto = CharacterInstanceSummary(instanceId="inst_1", characterId="elysia")
    assert dto.instanceId == "inst_1"
    assert dto.characterId == "elysia"
    assert dto.revision == 1
    assert dto.stateVersion == 1
    rt = CharacterInstanceSummary.model_validate(dto.model_dump())
    assert rt.model_dump() == dto.model_dump()


def test_character_public_state_dto():
    dto = CharacterPublicState(characterId="elysia", instanceId="inst_1")
    assert dto.characterId == "elysia"
    assert dto.instanceId == "inst_1"
    assert dto.characterRevision == 1
    assert dto.mood == "平静"
    assert dto.stateVersion == 1
    # wire_dict 同步可用（lib 序列化路径）
    assert dto.wire_dict()["characterId"] == "elysia"
    rt = CharacterPublicState.model_validate(dto.model_dump())
    assert rt.model_dump() == dto.model_dump()
