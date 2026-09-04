# core/cognitive/tests/test_turn_context.py
from dataclasses import FrozenInstanceError
import pytest
from core.cognitive.turn_context import TurnCharacterContext
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId

pytestmark = pytest.mark.unit   # 纯 dataclass


def test_context_is_frozen():
    ctx = TurnCharacterContext(
        conversationId="conv_1", characterInstanceId="chi_1",
        characterId="chr_elysia", characterRevision=1, userPronoun="她",
    )
    assert ctx.userPronoun == "她"
    with pytest.raises(FrozenInstanceError):
        ctx.userPronoun = "你"


def test_turn_context_extended_fields():
    st = CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s"))
    ctx = TurnCharacterContext(
        conversationId="conv_1", characterInstanceId="chi_x", characterId="chr_x",
        characterRevision=1, userPronoun="她", memoryScope="chi_x",
        voice={"providerId": None, "voiceId": None}, displayName="X", stateSnapshot=st,
    )
    assert ctx.memoryScope == "chi_x"
    assert ctx.displayName == "X"
    assert ctx.stateSnapshot.instance_id == "chi_x"
    with pytest.raises(FrozenInstanceError):
        ctx.memoryScope = "mutate"
