"""阶段 G:CharacterPublicState 契约(unit)。"""
import pytest
from frontend_contract.ws_message import (
    CharacterStateChangedMessage, ServerInfoFeatures, ServerMessage,
)
from frontend_contract.rest import CharacterPublicStateView, MoodState

pytestmark = pytest.mark.unit
_TS = "2026-09-05T00:00:00Z"


class TestMoodState:
    def test_mood_state_fields(self):
        m = MoodState(label="开心", intensity=0.72, updatedAt="2026-09-05T00:00:00Z")
        assert m.label == "开心" and m.intensity == 0.72


class TestPublicStateView:
    def test_view_fields(self):
        v = CharacterPublicStateView(
            mood=MoodState(label="开心", intensity=0.72, updatedAt=_TS),
            currentActivity=None, lastInteractionAt=None,
            proactiveEnabled=False, updatedAt=_TS)
        assert v.proactiveEnabled is False

    def test_view_wire_dict(self):
        v = CharacterPublicStateView(
            mood=MoodState(label="开心", intensity=0.72, updatedAt=_TS),
            proactiveEnabled=False, updatedAt=_TS)
        d = v.wire_dict()
        assert d["mood"]["label"] == "开心"
        assert "currentActivity" not in d  # None 缺省省略


class TestCharacterStateChangedMessage:
    def test_type_literal(self):
        msg = CharacterStateChangedMessage(
            characterId="chr_elysia", characterInstanceId="chi_builtin_elysia",
            stateVersion=3, state=CharacterPublicStateView(
                mood=MoodState(label="开心", intensity=0.72, updatedAt=_TS),
                proactiveEnabled=False, updatedAt=_TS))
        assert msg.type == "character.state.changed"

    def test_is_server_message_member(self):
        from typing import get_args, get_origin
        from typing import Annotated, Union
        origin = get_origin(ServerMessage)
        inner = get_args(ServerMessage)[0]
        members = get_args(inner) if get_origin(inner) is Union else [inner]
        assert CharacterStateChangedMessage in members


class TestServerInfoFeaturesCharacterStateStream:
    def test_default_false(self):
        assert ServerInfoFeatures().character_state_stream is False

    def test_explicit_true(self):
        f = ServerInfoFeatures(character_state_stream=True)
        assert f.character_state_stream is True
