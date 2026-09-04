# core/cognitive/tests/conftest.py
"""cognitive 测试共享 fixture/helper。

Task 8 起 `_prepare_turn` 从 session.character_binding 经
`CharacterRuntimeRegistry.get(instance_id, factory)` 解析 instance 到 runtime，
**不再 attach_session**。直接构造 `CognitiveLoop` 的测试需注入带 default runtime
的 registry，否则 `_prepare_turn` 因 `self._registry is None` 抛 RuntimeError。

本模块提供共享 helper，供各测试文件的 loop 构造复用。
"""

from unittest.mock import MagicMock

from core.cognitive.loop import DEFAULT_INSTANCE_ID
from core.character.registry import CharacterRuntimeRegistry
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity


def make_default_rt(*, instance_id: str = DEFAULT_INSTANCE_ID,
                    character_id: str = "chr_builtin_elysia",
                    revision: int = 1, display_name: str = "Elysia") -> CharacterRuntime:
    """构造带 engine 替身的 elysia default CharacterRuntime（registry 预置用）。

    测试不关心角色值，只需一个能通过 `_prepare_turn` 的 runtime。
    """
    rt = CharacterRuntime(
        instance=CharacterInstance(instanceId=instance_id, characterId=character_id),
        definition=CharacterDefinition(characterId=character_id),
        revision=CharacterRevision(
            characterId=character_id, revision=revision, displayName=display_name),
        user_identity=UserIdentity(userId="usr_local_default", displayName="妖梦"),
    )
    rt.ensure_engine(lambda: MagicMock())   # 预置 engine：_prepare_turn 不再触发 engine_factory
    return rt


def make_default_registry() -> CharacterRuntimeRegistry:
    """registry 预置 elysia default runtime（Task 8 后 _prepare_turn 需 registry.get）。"""
    reg = CharacterRuntimeRegistry()
    reg.get(DEFAULT_INSTANCE_ID, lambda: make_default_rt())
    return reg
