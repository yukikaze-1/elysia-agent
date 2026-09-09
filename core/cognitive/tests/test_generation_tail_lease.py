# core/cognitive/tests/test_generation_tail_lease.py
"""P0-1：generation busy 必须覆盖后台收尾（_after_response）。

修复前：process() 返回时即释放 lease，而 _archive_final_turn 用 create_task 启动的
_after_response（记忆沉淀 / CharacterReflector / MemoryReflector / 会话保存）仍在后台
运行——reset 可以在后台 task 真正取得 reflector/compression 锁之前插入。

本文件锁定 tail lease 的语义：主回合结束 + generation.completed 已发出后，只要后台
收尾未完成，reset 仍须被拒；tail task 异常/取消/shutdown 都必须释放 lease。
"""

import asyncio
from types import SimpleNamespace
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
from core.cognitive.generation import GenerationContext
from core.cognitive.loop import DEFAULT_INSTANCE_ID
from core.memory.session import SessionMemory
from core.monitoring.event_bus import EventBus
from core.profile.user_identity import UserIdentity
from core.schema.types import GenerationId, MessageId, SessionId
from core.status.agent_fsm import AgentFSM

pytestmark = pytest.mark.service


class _FreeLock:
    """未持有的锁替身——evolution_locks_busy 只读 `locked()`。"""

    def locked(self) -> bool:
        return False


def _rt() -> CharacterRuntime:
    rt = CharacterRuntime(
        instance=CharacterInstance(
            instanceId=DEFAULT_INSTANCE_ID, characterId="chr_builtin_elysia"),
        definition=CharacterDefinition(characterId="chr_builtin_elysia"),
        revision=CharacterRevision(
            characterId="chr_builtin_elysia", revision=1, displayName="Elysia"),
        user_identity=UserIdentity(userId="usr_local_default", displayName="妖梦"),
    )
    rt.bind_engine(SimpleNamespace(
        _reflector=SimpleNamespace(_reflect_lock=_FreeLock()),
        _compression=SimpleNamespace(_compression_lock=_FreeLock()),
    ))
    return rt


def _make_loop(rt: CharacterRuntime) -> CognitiveLoop:
    reg = CharacterRuntimeRegistry()
    reg.get(DEFAULT_INSTANCE_ID, lambda: rt)
    llm = MagicMock(spec=LLMAdapter)
    llm.model = "mock"
    prompt = MagicMock()
    prompt._character = MagicMock()
    prompt._character.name = "TestBot"
    prompt.build_messages = AsyncMock(
        return_value=[{"role": "user", "content": "hi"}])
    default = SessionMemory(session_id="default")

    async def resolver(conversation_id, protocol_version):
        return default

    return CognitiveLoop(
        llm=llm, fsm=AgentFSM(), event_bus=EventBus(), output=OutputManager(),
        prompt=prompt, session_resolver=resolver, runtime_registry=reg,
    )


def _target_session() -> MagicMock:
    target = MagicMock()
    target.session_id = "default"
    target.save = AsyncMock()
    return target


async def _run_archive(loop: CognitiveLoop, target: MagicMock) -> asyncio.Task:
    """触发 _archive_final_turn（真实收尾路径），返回其创建的 tail task。"""
    await loop._archive_final_turn(
        acc_text="hi", all_sentences=[], blocks=[], iv_think_buf="",
        public_text="hi", user_message="hello", round_num=0, target=target)
    pending = [t for t in loop._pending_after_response if not t.done()]
    assert len(pending) == 1
    return pending[0]


@pytest.mark.asyncio
async def test_tail_lease_blocks_reset_after_turn_completed():
    """consolidator 被卡住时，即使主回合与 generation.completed 已完成，reset 仍被拒。"""
    rt = _rt()
    loop = _make_loop(rt)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def consolidate(user_message, assistant_response):
        started.set()
        await gate.wait()
        return []

    rt.consolidator = MagicMock(consolidate=consolidate)
    rt.reflector = None

    assert rt.try_begin_generation() is True
    loop._current_runtime = rt
    loop._generation_ctx = GenerationContext(
        generation_id=GenerationId("gen_tail"),
        user_message_id=MessageId("msg_user"),
        assistant_message_id=MessageId("msg_assistant"),
        conversation_id=SessionId("default"),
        model_id="mock",
    )
    await loop._mark_generation_completed()
    assert loop._generation_completed is True
    target = _target_session()

    tail = await _run_archive(loop, target)
    await started.wait()

    # 主回合 lease 已释放（process 已返回），但 tail lease 仍在 → 不得放行 reset。
    rt.end_generation()
    assert rt.is_generation_busy is True
    assert rt.try_begin_destructive_mutation() is False

    gate.set()
    await asyncio.wait_for(tail, timeout=5)
    assert rt.is_generation_busy is False
    assert rt.try_begin_destructive_mutation() is True
    rt.end_destructive_mutation()
    target.save.assert_awaited()
    await loop._output.close()


@pytest.mark.asyncio
async def test_tail_lease_released_when_after_response_raises():
    """收尾内部异常（记忆沉淀失败）不得泄漏 lease——否则 instance 永久卡死。"""
    rt = _rt()
    loop = _make_loop(rt)

    async def boom(user_message, assistant_response):
        raise RuntimeError("consolidate boom")

    rt.consolidator = MagicMock(consolidate=boom)
    rt.reflector = None
    assert rt.try_begin_generation() is True
    loop._current_runtime = rt
    target = _target_session()

    tail = await _run_archive(loop, target)
    rt.end_generation()
    await asyncio.wait_for(tail, timeout=5)

    assert rt.is_generation_busy is False
    assert rt.try_begin_destructive_mutation() is True
    rt.end_destructive_mutation()
    await loop._output.close()


@pytest.mark.asyncio
async def test_tail_lease_released_when_tail_task_cancelled():
    """shutdown/flush 超时取消 tail task → lease 必须释放。"""
    rt = _rt()
    loop = _make_loop(rt)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def consolidate(user_message, assistant_response):
        started.set()
        await gate.wait()
        return []

    rt.consolidator = MagicMock(consolidate=consolidate)
    rt.reflector = None
    assert rt.try_begin_generation() is True
    loop._current_runtime = rt
    target = _target_session()

    tail = await _run_archive(loop, target)
    await started.wait()
    rt.end_generation()
    assert rt.is_generation_busy is True

    tail.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tail
    assert rt.is_generation_busy is False
    await loop._output.close()


@pytest.mark.asyncio
async def test_tail_lease_released_on_shutdown_skip():
    """shutdown 门禁令 _after_response 提前退出时，tail lease 必须立即释放。"""
    rt = _rt()
    loop = _make_loop(rt)
    rt.consolidator = MagicMock(consolidate=AsyncMock(return_value=[]))
    rt.reflector = None
    assert rt.try_begin_generation() is True
    loop._current_runtime = rt
    loop.mark_shutting_down()
    target = _target_session()

    tail = await _run_archive(loop, target)
    rt.end_generation()
    await asyncio.wait_for(tail, timeout=5)

    assert rt.is_generation_busy is False
    rt.consolidator.consolidate.assert_not_awaited()
    await loop._output.close()


@pytest.mark.asyncio
async def test_tail_captures_turn_engine_not_next_turn_engine():
    """P0-1：tail task 必须用本回合捕获的 engine——晚读 self._engine 会把 A 轮
    的 buffer_message/CharacterReflector 写进下一轮 B 角色的 engine。

    复现：A 轮创建 tail task 后、tail 真正执行前，立即把 loop._engine 换成 engine_b。
    """
    rt = _rt()
    loop = _make_loop(rt)
    rt.consolidator = None
    rt.reflector = None

    engine_a = MagicMock()
    engine_a.buffer_message = MagicMock(return_value=None)
    engine_b = MagicMock()
    engine_b.buffer_message = MagicMock(return_value=None)
    loop._engine = engine_a

    assert rt.try_begin_generation() is True
    loop._current_runtime = rt
    target = _target_session()

    tail = await _run_archive(loop, target)
    # tail 已创建但尚未执行——模拟下一轮 _prepare_turn 换绑 engine
    loop._engine = engine_b
    rt.end_generation()
    await asyncio.wait_for(tail, timeout=5)

    engine_a.buffer_message.assert_called_once()
    engine_b.buffer_message.assert_not_called()
    assert rt.is_generation_busy is False
    await loop._output.close()


@pytest.mark.asyncio
async def test_tail_lease_awaits_reflector_task():
    """engine.buffer_message 返回的反思 task 必须被 tail 持有到结束。"""
    rt = _rt()
    loop = _make_loop(rt)
    rt.consolidator = None
    reflect_done = asyncio.Event()

    async def reflect_job():
        await asyncio.sleep(0)
        reflect_done.set()

    engine = MagicMock()
    engine.buffer_message = MagicMock(
        return_value=asyncio.create_task(reflect_job()))
    loop._engine = engine
    assert rt.try_begin_generation() is True
    loop._current_runtime = rt
    target = _target_session()

    tail = await _run_archive(loop, target)
    rt.end_generation()
    await asyncio.wait_for(tail, timeout=5)

    assert reflect_done.is_set()
    assert rt.is_generation_busy is False
    await loop._output.close()


@pytest.mark.asyncio
async def test_process_refuses_generation_while_destructive_mutation_active():
    """对称互斥：reset 在途时 process 不得开始 generation（领域层独立守住不变量）。"""
    rt = _rt()
    loop = _make_loop(rt)
    loop._handle_turn_exception = AsyncMock()  # type: ignore[method-assign]
    think = AsyncMock()
    loop._think_loop = think  # type: ignore[method-assign]

    assert rt.try_begin_destructive_mutation() is True
    try:
        await loop.process("hello")
    finally:
        rt.end_destructive_mutation()

    think.assert_not_awaited()
    loop._handle_turn_exception.assert_awaited_once()
    exc = loop._handle_turn_exception.await_args.args[0]
    assert type(exc).__name__ == "CharacterBusyError"
    assert rt.is_generation_busy is False
    await loop._output.close()
