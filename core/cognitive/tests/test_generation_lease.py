# core/cognitive/tests/test_generation_lease.py
"""BE-STAGEH-01：CognitiveLoop.process 持有/释放 runtime 的 generation lease。

真实联调复现：generation 在途时 reset-evolution 未被拒绝。lease 的持有范围必须覆盖
整个回合（含回合边界的状态收尾），并在异常/取消路径由 finally 释放。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from Module.output import OutputManager
from core.adapter.llm_adapter import LLMAdapter
from core.character.definition import CharacterDefinition
from core.character.instance import CharacterInstance
from core.character.registry import CharacterRuntimeRegistry
from core.character.revision import CharacterRevision
from core.character.runtime import CharacterRuntime
from core.cognitive import CognitiveLoop
from core.cognitive.loop import DEFAULT_INSTANCE_ID
from core.memory.session import SessionMemory
from core.monitoring.event_bus import EventBus
from core.profile.user_identity import UserIdentity
from core.status.agent_fsm import AgentFSM

pytestmark = pytest.mark.service


def _rt() -> CharacterRuntime:
    rt = CharacterRuntime(
        instance=CharacterInstance(
            instanceId=DEFAULT_INSTANCE_ID, characterId="chr_builtin_elysia"),
        definition=CharacterDefinition(characterId="chr_builtin_elysia"),
        revision=CharacterRevision(
            characterId="chr_builtin_elysia", revision=1, displayName="Elysia"),
        user_identity=UserIdentity(userId="usr_local_default", displayName="妖梦"),
    )
    from core.cognitive.tests.conftest import mock_engine_with_state
    rt.ensure_engine(
        lambda: mock_engine_with_state(instance_id=DEFAULT_INSTANCE_ID))
    return rt


def _make_loop(rt: CharacterRuntime) -> CognitiveLoop:
    reg = CharacterRuntimeRegistry()
    reg.get(DEFAULT_INSTANCE_ID, lambda: rt)
    llm = MagicMock(spec=LLMAdapter)
    llm.model = "mock"
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    prompt.build_messages = AsyncMock(
        return_value=[{"role": "user", "content": "hi"}])
    default = SessionMemory(session_id="default")

    async def resolver(conversation_id, protocol_version):
        return default

    return CognitiveLoop(
        llm=llm, fsm=AgentFSM(), event_bus=EventBus(), output=OutputManager(),
        prompt=prompt, session_resolver=resolver, runtime_registry=reg,
    )


@pytest.mark.asyncio
async def test_process_holds_generation_lease_until_turn_finishes():
    rt = _rt()
    loop = _make_loop(rt)
    seen: list[bool] = []

    async def fake_think(*args, **kwargs):
        seen.append(rt.is_generation_busy)

    loop._think_loop = fake_think  # type: ignore[method-assign]
    await loop.process("hello")

    assert seen == [True]
    assert rt.is_generation_busy is False
    await loop._output.close()


@pytest.mark.asyncio
async def test_process_releases_lease_after_turn_exception():
    rt = _rt()
    loop = _make_loop(rt)

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    loop._think_loop = boom  # type: ignore[method-assign]
    loop._handle_turn_exception = AsyncMock()  # type: ignore[method-assign]
    await loop.process("hello")

    assert rt.is_generation_busy is False
    loop._handle_turn_exception.assert_awaited_once()
    await loop._output.close()


@pytest.mark.asyncio
async def test_process_releases_lease_after_cancel():
    import asyncio

    rt = _rt()
    loop = _make_loop(rt)
    started = asyncio.Event()

    async def blocking_think(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)

    loop._think_loop = blocking_think  # type: ignore[method-assign]
    task = asyncio.create_task(loop.process("hello"))
    await started.wait()
    assert rt.is_generation_busy is True

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass  # process() 内部吞掉 CancelledError 走 _handle_interrupt，两种结果都可接受

    assert rt.is_generation_busy is False
    await loop._output.close()
