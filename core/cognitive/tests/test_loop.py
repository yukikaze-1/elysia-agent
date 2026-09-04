# core/cognitive/tests/test_loop.py
"""Task 7/8（B1 闭环）：_prepare_turn 冻结 TurnCharacterContext 并传给 prompt。

覆盖 plan Task 7 Step 5 + Task 8：
- _prepare_turn 后 self._turn_context 非 None；
- self._prompt._turn_context is self._turn_context（B1 闭环关键）；
- userPronoun 取自 UserIdentityStore（configure 注入）——缺 store 时兜底「她」；
- Task 8：_prepare_turn 从 session.character_binding 解析 instance_id，经
  registry.get → runtime.ensure_engine → self._engine；context 字段取自 runtime。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.status.agent_fsm import AgentFSM
from core.monitoring.event_bus import EventBus
from Module.output import OutputManager
from core.memory.session import SessionMemory
from core.cognitive import CognitiveLoop
from core.cognitive.turn_context import TurnCharacterContext
from core.cognitive.loop import DEFAULT_INSTANCE_ID
from core.adapter.llm_adapter import LLMAdapter
from core.schema import SessionId
from core.character.registry import CharacterRuntimeRegistry
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity

pytestmark = pytest.mark.service


def _make_loop(*, engine=None, user_identity_store=None,
               on_store: bool = True, runtime_registry=None,
               runtime_factory=None, engine_factory=None) -> CognitiveLoop:
    """构造最小 CognitiveLoop（不跑 process，仅测 _prepare_turn）。

    resolver 恒返回 default 会话；prompt 用 MagicMock（set_session 为同步方法）。
    """
    llm = MagicMock(spec=LLMAdapter)
    llm.model = "mock"
    output = OutputManager()
    fsm = AgentFSM()
    bus = EventBus()
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    prompt._turn_context = None
    prompt.set_session = MagicMock()
    prompt.build_messages = AsyncMock(return_value=[
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ])
    default = SessionMemory(session_id="default")

    async def resolver(conversation_id, protocol_version):
        return default

    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt, session_resolver=resolver,
        character_engine=engine,
        runtime_registry=runtime_registry,
        runtime_factory=runtime_factory,
        engine_factory=engine_factory,
    )
    if on_store:
        loop.configure(user_identity_store=user_identity_store)
    return loop


def _make_rt(*, instance_id: str = "chi_builtin_elysia",
             character_id: str = "chr_builtin_elysia",
             revision: int = 1, display_name: str = "Elysia") -> CharacterRuntime:
    """构造带 engine 替身的 CharacterRuntime（registry 预置用）。"""
    rt = CharacterRuntime(
        instance=CharacterInstance(instanceId=instance_id, characterId=character_id),
        definition=CharacterDefinition(characterId=character_id),
        revision=CharacterRevision(
            characterId=character_id, revision=revision, displayName=display_name),
        user_identity=UserIdentity(userId="usr_local_default", displayName="妖梦"),
    )
    from core.cognitive.tests.conftest import mock_engine_with_state
    rt.ensure_engine(lambda: mock_engine_with_state(instance_id=instance_id))   # 预置 engine：_prepare_turn 不再触发 engine_factory
    return rt


def _seed_registry(reg: CharacterRuntimeRegistry, rt: CharacterRuntime,
                   instance_id: str) -> CharacterRuntimeRegistry:
    """把 runtime 预置进 registry（miss 时不触发 runtime_factory）。"""
    reg.get(instance_id, lambda: rt)
    return reg


def _store_with_identity(pronouns: str, *, identity: bool = True) -> MagicMock:
    """构造 store 替身：get() 返回带给定 pronouns 的 identity；identity=False 时返回 None。"""
    store = MagicMock()
    if identity:
        ident = MagicMock()
        ident.pronouns = pronouns
        store.get.return_value = ident
    else:
        store.get.return_value = None
    return store


@pytest.mark.asyncio
async def test_prepare_turn_freezes_turn_context():
    """_prepare_turn 后 self._turn_context 非 None，且 prompt._turn_context 指向同一对象。"""
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    _seed_registry(reg, rt, rt.instance.instanceId)
    loop = _make_loop(
        runtime_registry=reg, runtime_factory=None, engine_factory=None,
        user_identity_store=_store_with_identity("她"),
        on_store=True,
    )
    setup = await loop._prepare_turn(
        user_message="你好", user_name="u", stt_emotion="", content=None,
        message_id=None, conversation_id=None, protocol_version=1,
    )
    assert setup is not None
    ctx = loop._turn_context
    assert isinstance(ctx, TurnCharacterContext)
    assert ctx.characterInstanceId == rt.instance.instanceId
    assert ctx.characterId == rt.definition.characterId
    assert ctx.characterRevision == 1
    assert ctx.userPronoun == "她"       # store 注入 identity.pronouns="她"
    assert loop._prompt._turn_context is ctx   # B1 闭环关键：同一对象传给 prompt
    await loop._output.close()


@pytest.mark.asyncio
async def test_prepare_turn_reads_user_pronoun_from_store():
    """userPronoun 取自 UserIdentityStore.get().pronouns（B1 全局唯一「她」）。"""
    identity = MagicMock()
    identity.pronouns = "她"
    store = MagicMock()
    store.get.return_value = identity
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    _seed_registry(reg, rt, rt.instance.instanceId)
    loop = _make_loop(
        runtime_registry=reg, runtime_factory=None, engine_factory=None,
        user_identity_store=store, on_store=True,
    )
    setup = await loop._prepare_turn(
        user_message="你好", user_name="u", stt_emotion="", content=None,
        message_id=SessionId("conv_1"), conversation_id=SessionId("conv_1"),
        protocol_version=2,
    )
    assert setup is not None
    assert loop._turn_context.userPronoun == "她"
    assert loop._turn_context.conversationId == SessionId("conv_1")
    await loop._output.close()


@pytest.mark.asyncio
async def test_prepare_turn_without_binding_falls_back_to_default_instance():
    """session 无 character_binding 时 instance_id 回退 DEFAULT_INSTANCE_ID。"""
    reg = CharacterRuntimeRegistry()
    rt = _make_rt(instance_id=DEFAULT_INSTANCE_ID)   # 预置默认 instance 的 runtime
    _seed_registry(reg, rt, DEFAULT_INSTANCE_ID)
    loop = _make_loop(
        runtime_registry=reg, runtime_factory=None, engine_factory=None,
        user_identity_store=_store_with_identity("她"), on_store=True,
    )
    sm = SessionMemory(session_id=SessionId("default"))
    loop._session_resolver = AsyncMock(return_value=sm)
    setup = await loop._prepare_turn(
        user_message="你好", user_name="u", stt_emotion="", content=None,
        message_id=None, conversation_id=None, protocol_version=1,
    )
    assert setup is not None
    ctx = loop._turn_context
    assert ctx.characterInstanceId == DEFAULT_INSTANCE_ID
    assert ctx.characterId == rt.definition.characterId
    assert ctx.characterRevision == 1
    assert ctx.userPronoun == "她"
    assert loop._engine is rt.engine
    await loop._output.close()


@pytest.mark.asyncio
async def test_prepare_turn_without_store_falls_back_to_default_pronoun():
    """无 user_identity_store 时 _resolve_pronouns 兜底「她」。"""
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    _seed_registry(reg, rt, rt.instance.instanceId)
    loop = _make_loop(runtime_registry=reg, runtime_factory=None, engine_factory=None,
                      on_store=False)
    loop._user_identity_store = None
    setup = await loop._prepare_turn(
        user_message="你好", user_name="u", stt_emotion="", content=None,
        message_id=None, conversation_id=None, protocol_version=1,
    )
    assert setup is not None
    assert loop._turn_context.userPronoun == "她"
    await loop._output.close()


def test_loop_injects_runtime_registry():
    """Task 7（B1）：__init__ 注入 runtime_registry/runtime_factory/engine_factory，
    且 mixin 改名 _engine —— 不再有 _character_engine 属性。"""
    from core.character.registry import CharacterRuntimeRegistry
    reg = CharacterRuntimeRegistry()
    loop = _make_loop(runtime_registry=reg, runtime_factory=lambda iid: None, engine_factory=lambda rt: None)
    assert loop._registry is reg
    assert loop._runtime_factory is not None and loop._engine_factory is not None
    assert not hasattr(loop, "_character_engine")   # B1: mixin 改名 _engine


@pytest.mark.asyncio
async def test_prepare_turn_resolves_from_binding():
    """Task 8：_prepare_turn 从 session.character_binding 解析 instance_id，
    经 registry.get → runtime.ensure_engine → self._engine；context 字段取自 runtime。"""
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    _seed_registry(reg, rt, rt.instance.instanceId)
    loop = _make_loop(runtime_registry=reg)
    sm = SessionMemory(session_id=SessionId("default"))
    sm.character_binding = {
        "character_id": rt.definition.characterId,
        "instance_id": rt.instance.instanceId,
    }
    loop._session_resolver = AsyncMock(return_value=sm)
    loop._session = sm
    setup = await loop._prepare_turn(
        user_message="你好", user_name="妖梦", stt_emotion="", content=None,
        message_id=None, conversation_id=None, protocol_version=1,
    )
    assert setup is not None
    assert loop._engine is rt.engine
    assert loop._turn_context.characterInstanceId == rt.instance.instanceId
    assert loop._turn_context.characterId == rt.definition.characterId
    await loop._output.close()


@pytest.mark.asyncio
async def test_prepare_turn_sets_current_runtime_for_turn_boundary_flush():
    """M3（I1）：_prepare_turn 记录 self._current_runtime——回合边界 flush 据此触发。"""
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    _seed_registry(reg, rt, rt.instance.instanceId)
    loop = _make_loop(runtime_registry=reg)
    sm = SessionMemory(session_id=SessionId("default"))
    loop._session_resolver = AsyncMock(return_value=sm)
    await loop._prepare_turn(
        user_message="你好", user_name="妖梦", stt_emotion="", content=None,
        message_id=None, conversation_id=None, protocol_version=1,
    )
    assert loop._current_runtime is rt
    await loop._output.close()


@pytest.mark.asyncio
async def test_turn_boundary_flush_syncs_reflector_and_persists():
    """M3（I1）：_flush_runtime_at_turn_boundary 同步 reflector→runtime.state 并 flush。

    runtime 带 mock persist_fn，engine 为 mock（sync_reflector_to_runtime_state 可断言）。
    """
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    _seed_registry(reg, rt, rt.instance.instanceId)
    persist = AsyncMock()
    rt.set_persist_fn(persist)
    loop = _make_loop(runtime_registry=reg)
    sm = SessionMemory(session_id=SessionId("default"))
    loop._session_resolver = AsyncMock(return_value=sm)
    await loop._prepare_turn(
        user_message="你好", user_name="妖梦", stt_emotion="", content=None,
        message_id=None, conversation_id=None, protocol_version=1,
    )
    loop._current_runtime = rt
    loop._engine.sync_reflector_to_runtime_state = MagicMock()
    await loop._flush_runtime_at_turn_boundary()
    loop._engine.sync_reflector_to_runtime_state.assert_called_once()  # 同步方法
    persist.assert_awaited_once()  # runtime.flush() 经 persist_fn 落库
    await loop._output.close()


@pytest.mark.asyncio
async def test_turn_boundary_flush_no_runtime_is_safe():
    """M3（I1）：无 _current_runtime（直接构造 loop 的测试路径）时 _flush 静默跳过。"""
    reg = CharacterRuntimeRegistry()
    rt = _make_rt()
    _seed_registry(reg, rt, rt.instance.instanceId)
    loop = _make_loop(runtime_registry=reg)
    loop._current_runtime = None
    await loop._flush_runtime_at_turn_boundary()  # 不抛
    await loop._output.close()
