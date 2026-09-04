# core/cognitive/tests/test_loop.py
"""Task 7（B1 闭环）：_prepare_turn 冻结 TurnCharacterContext 并传给 prompt。

覆盖 plan Task 7 Step 5：
- _prepare_turn 后 self._turn_context 非 None；
- self._prompt._turn_context is self._turn_context（B1 闭环关键）；
- userPronoun 取自 UserIdentityStore（configure 注入）——缺 store 时兜底「她」。
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
from core.adapter.llm_adapter import LLMAdapter
from core.schema import SessionId

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


def _make_engine(*, instance_id: str = "chi_builtin_testbot",
                 revision: int = 1, name: str = "TestBot") -> MagicMock:
    """构造 engine 替身：带 _instance_id/_character_revision/_character/_instance_id 访问。"""
    engine = MagicMock()
    engine._instance_id = instance_id
    engine._character_revision = revision
    engine._character = MagicMock()
    engine._character.name = name
    return engine


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
    engine = _make_engine()
    loop = _make_loop(
        engine=engine,
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
    assert ctx.characterInstanceId == "chi_builtin_testbot"
    assert ctx.characterId == "testbot"
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
    loop = _make_loop(engine=_make_engine(), user_identity_store=store, on_store=True)
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
async def test_prepare_turn_without_engine_falls_back_to_defaults():
    """无 character_engine 时 context 字段回退默认（chi_default/agent/1）。"""
    loop = _make_loop(engine=None, user_identity_store=_store_with_identity("她"), on_store=True)
    setup = await loop._prepare_turn(
        user_message="你好", user_name="u", stt_emotion="", content=None,
        message_id=None, conversation_id=None, protocol_version=1,
    )
    assert setup is not None
    ctx = loop._turn_context
    assert ctx.characterInstanceId == "chi_default"
    assert ctx.characterId == "agent"
    assert ctx.characterRevision == 1
    assert ctx.userPronoun == "她"
    await loop._output.close()


@pytest.mark.asyncio
async def test_prepare_turn_without_store_falls_back_to_default_pronoun():
    """无 user_identity_store 时 _resolve_pronouns 兜底「她」。"""
    loop = _make_loop(engine=_make_engine(), on_store=False)
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
