import pytest

from core.character.binding import normalize_character_binding

pytestmark = pytest.mark.unit


def test_normalize_character_binding_accepts_canonical_and_legacy_keys():
    expected = {"character_id": "chr_a", "instance_id": "chi_a"}
    assert normalize_character_binding(expected) == expected
    assert normalize_character_binding({
        "characterId": " chr_a ", "instanceId": " chi_a ", "extra": True,
    }) == expected


@pytest.mark.parametrize("value", [None, {}, [], "x", {"character_id": "chr_a"}])
def test_normalize_character_binding_rejects_partial_or_invalid_values(value):
    assert normalize_character_binding(value) is None
