# core/cognitive/tests/test_turn_context.py
from dataclasses import FrozenInstanceError
import pytest
from core.cognitive.turn_context import TurnCharacterContext

pytestmark = pytest.mark.unit   # 纯 dataclass


def test_context_is_frozen():
    ctx = TurnCharacterContext(
        conversationId="conv_1", characterInstanceId="chi_1",
        characterId="chr_elysia", characterRevision=1, userPronoun="她",
    )
    assert ctx.userPronoun == "她"
    with pytest.raises(FrozenInstanceError):
        ctx.userPronoun = "你"
