# core/cognitive/tests/test_identity_snapshot.py
"""P0-2：_prepare_turn 冻结本轮权威 UserIdentity 快照。

覆盖：
- 快照字段完整进入 TurnCharacterContext（displayName/preferredAddress/pronouns/
  locale/timezone/revision）；
- 改档案后新一轮采用新 revision，旧的在途上下文保持不变（frozen）；
- 有效称呼投影到 per-runtime 记忆组件（consolidator/reflector/性格反思）；
- 身份自由文本不进入普通日志。
"""

import logging
from unittest.mock import MagicMock

import pytest

from core.cognitive.loop import DEFAULT_INSTANCE_ID
from core.cognitive.tests.test_loop import _make_loop, _make_rt, _seed_registry
from core.character.registry import CharacterRuntimeRegistry
from core.profile.user_identity import UserIdentity

pytestmark = pytest.mark.service


def _store(identity: UserIdentity | None) -> MagicMock:
    store = MagicMock()
    store.get.return_value = identity
    return store


def _identity(revision: int = 1, **kwargs) -> UserIdentity:
    base = dict(
        userId="usr_local_default",
        displayName="妖梦",
        preferredAddress="主人",
        pronouns="她",
        locale="zh-CN",
        timezone="Asia/Shanghai",
    )
    base.update(kwargs)
    identity = UserIdentity(**base)
    identity.revision = revision
    return identity


async def _prepare(loop, *, user_name: str = "caller"):
    return await loop._prepare_turn(
        user_message="你好", user_name=user_name, stt_emotion="", content=None,
        message_id=None, conversation_id=None, protocol_version=1,
    )


@pytest.mark.asyncio
async def test_prepare_turn_freezes_full_identity_snapshot():
    """快照字段逐项进入 TurnCharacterContext，revision 为权威 profile 的 revision。"""
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    _seed_registry(reg, rt, rt.instance.instanceId)
    loop = _make_loop(
        runtime_registry=reg, runtime_factory=None, engine_factory=None,
        user_identity_store=_store(_identity(revision=7)), on_store=True,
    )
    setup = await _prepare(loop)
    assert setup is not None
    ctx = loop._turn_context
    assert ctx.userId == "usr_local_default"
    assert ctx.userDisplayName == "妖梦"
    assert ctx.userPreferredAddress == "主人"
    assert ctx.userPronouns == "她"
    assert ctx.userLocale == "zh-CN"
    assert ctx.userTimezone == "Asia/Shanghai"
    assert ctx.userIdentityRevision == 7
    assert ctx.effectiveUserAddress == "主人"
    await loop._output.close()


@pytest.mark.asyncio
async def test_new_turn_adopts_new_revision_while_in_flight_context_frozen():
    """改档案后新一轮用新 revision；旧的在途上下文对象保持不变。"""
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    _seed_registry(reg, rt, rt.instance.instanceId)
    store = _store(_identity(revision=1))
    loop = _make_loop(
        runtime_registry=reg, runtime_factory=None, engine_factory=None,
        user_identity_store=store, on_store=True,
    )
    await _prepare(loop)
    first = loop._turn_context
    assert first is not None and first.userIdentityRevision == 1
    assert first.effectiveUserAddress == "主人"

    store.get.return_value = _identity(
        revision=2, displayName="妖梦", preferredAddress="大小姐")
    await _prepare(loop)
    second = loop._turn_context
    assert second is not None and second.userIdentityRevision == 2
    assert second.effectiveUserAddress == "大小姐"
    # 在途上下文是 frozen 快照——不被新一轮覆盖。
    assert first.userIdentityRevision == 1
    assert first.effectiveUserAddress == "主人"
    await loop._output.close()


@pytest.mark.asyncio
async def test_identity_address_projected_to_runtime_memory_components():
    """有效称呼投影到 consolidator / MemoryReflector / CharacterReflector。"""
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    rt.consolidator = MagicMock()
    rt.reflector = MagicMock()
    _seed_registry(reg, rt, rt.instance.instanceId)
    loop = _make_loop(
        runtime_registry=reg, runtime_factory=None, engine_factory=None,
        user_identity_store=_store(_identity(preferredAddress="主人")),
        on_store=True,
    )
    await _prepare(loop)

    rt.consolidator.set_user_name.assert_called_once_with("主人")
    rt.reflector.set_user_name.assert_called_once_with("主人")
    rt.engine.reflector.set_user_name.assert_called_once_with("主人")
    await loop._output.close()


@pytest.mark.asyncio
async def test_identity_free_text_not_in_logs(caplog):
    """身份自由文本（displayName/preferredAddress）不得进入普通日志。"""
    marker = "IDENTITY_MARKER_7f3a"
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    _seed_registry(reg, rt, rt.instance.instanceId)
    loop = _make_loop(
        runtime_registry=reg, runtime_factory=None, engine_factory=None,
        user_identity_store=_store(
            _identity(displayName=marker, preferredAddress=marker + "_ADDR")),
        on_store=True,
    )
    with caplog.at_level(logging.DEBUG):
        setup = await _prepare(loop)
    assert setup is not None
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert marker not in rendered
    await loop._output.close()


@pytest.mark.asyncio
async def test_identity_fallback_without_store_keeps_caller_name():
    """无 store（单测/本地模式）时回退调用方用户名，代词用默认值。"""
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    _seed_registry(reg, rt, rt.instance.instanceId)
    loop = _make_loop(
        runtime_registry=reg, runtime_factory=None, engine_factory=None,
        user_identity_store=None, on_store=False,
    )
    loop._user_identity_store = None
    setup = await _prepare(loop, user_name="小妖")
    assert setup is not None
    ctx = loop._turn_context
    assert ctx.userDisplayName == "小妖"
    assert ctx.userPreferredAddress is None
    assert ctx.effectiveUserAddress == "小妖"
    assert ctx.userPronouns == "她"
    await loop._output.close()


@pytest.mark.asyncio
async def test_identity_snapshot_instance_matches_binding():
    """快照不改变既有 instance/character 归属语义。"""
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    _seed_registry(reg, rt, rt.instance.instanceId)
    loop = _make_loop(
        runtime_registry=reg, runtime_factory=None, engine_factory=None,
        user_identity_store=_store(_identity()), on_store=True,
    )
    setup = await _prepare(loop)
    assert setup is not None
    assert loop._turn_context.characterInstanceId == DEFAULT_INSTANCE_ID
    assert loop._turn_context.characterId == rt.definition.characterId
    await loop._output.close()
