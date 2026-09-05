# core/memory/tests/test_scope.py
import dataclasses

import pytest
from core.memory.scope import MemoryScope, MemoryVisibility


def test_scope_immutable():
    scope = MemoryScope(
        user_id="usr_local_default", character_instance_id="chi_builtin_elysia",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.user_id = "mutate"  # frozen dataclass 不可变
    assert scope.visibility == MemoryVisibility.CHARACTER_PRIVATE


def test_visibility_values():
    assert MemoryVisibility.CHARACTER_PRIVATE == "character_private"
    assert MemoryVisibility.USER_SHARED == "user_shared"
