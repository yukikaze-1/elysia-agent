"""CognitiveLoop generation 持久化挂点测试（批次 3 Task 9，marker=service）。

覆盖 loop.py 中 register/mark 挂点：
- process 成功 → register(running) → mark(completed, finish_reason=stop)
- process 错误路径 → mark(failed)（终态唯一，_handle_interrupt 不覆盖）
- cancel_current("disconnect") → mark(interrupted)
- cancel_current("vad") → mark(cancelled)
- 目标会话归属：conversation_id 解析出的 session 写 generation.session_id

使用真实 GenerationRegistry + 内存 FakeDocumentDBBackend（不碰真实 Mongo）。
"""

import asyncio
import contextlib

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.status.agent_fsm import AgentFSM
from core.monitoring.event_bus import EventBus
from Module.output import OutputManager
from core.memory.session import SessionMemory
from core.cognitive import CognitiveLoop
from core.cognitive.generation_registry import GenerationRegistry
from core.storage.generation_repo import GenerationRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.adapter.llm_adapter import LLMAdapter
from core.cognitive.loop import DEFAULT_INSTANCE_ID
from core.character.registry import CharacterRuntimeRegistry
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity

pytestmark = pytest.mark.service


def _default_rt() -> CharacterRuntime:
    """构造 elysia default runtime（registry 预置用，测试不关心角色值）。"""
    rt = CharacterRuntime(
        instance=CharacterInstance(instanceId=DEFAULT_INSTANCE_ID, characterId="chr_builtin_elysia"),
        definition=CharacterDefinition(characterId="chr_builtin_elysia"),
        revision=CharacterRevision(characterId="chr_builtin_elysia", revision=1, displayName="Elysia"),
        user_identity=UserIdentity(userId="usr_local_default", displayName="妖梦"),
    )
    rt.ensure_engine(lambda: MagicMock())   # 预置 engine：_prepare_turn 不再触发 engine_factory
    return rt


def _default_registry() -> CharacterRuntimeRegistry:
    """registry 预置 default runtime（Task 8 后 _prepare_turn 需 registry.get）。"""
    reg = CharacterRuntimeRegistry()
    reg.get(DEFAULT_INSTANCE_ID, lambda: _default_rt())
    return reg


def _make_loop(sessions: dict, default_session: SessionMemory,
               registry: GenerationRegistry,
               stream_impl=None) -> CognitiveLoop:
    """构造最小 CognitiveLoop，注入 registry + fake resolver + 可选 stream_impl。"""
    llm = MagicMock(spec=LLMAdapter)
    llm.model = "mock"
    if stream_impl is not None:
        llm.stream_think = stream_impl
    else:
        async def fake_stream(messages, tools=None, **kw):
            for c in "你好":
                yield {"content": c, "tool_calls": None}
        llm.stream_think = fake_stream

    output = OutputManager()
    fsm = AgentFSM()
    bus = EventBus()
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    prompt.build_messages = AsyncMock(return_value=[
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ])

    async def resolver(conversation_id, protocol_version):
        if protocol_version < 2 or conversation_id is None:
            return default_session
        return sessions.get(conversation_id, default_session)

    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt, session_resolver=resolver,
        generation_registry=registry,
        runtime_registry=_default_registry(),
    )
    return loop


def _registry() -> GenerationRegistry:
    return GenerationRegistry(repo=GenerationRepository(db=FakeDocumentDBBackend()))


async def _await_pending(loop: CognitiveLoop) -> None:
    """等待所有 pending after_response task 完成。"""
    pending = [t for t in loop._pending_after_response if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _session_docs(reg: GenerationRegistry, session_id: str) -> list[dict]:
    docs, _, _ = await reg.query_by_session(session_id)
    return docs


class TestGenerationWiring:
    @pytest.mark.asyncio
    async def test_success_registers_and_marks_completed(self):
        reg = _registry()
        loop = _make_loop({}, SessionMemory(session_id="default"), reg)
        await loop.process("hello")
        await _await_pending(loop)
        docs = await _session_docs(reg, "default")
        assert len(docs) == 1  # 每轮只建档一次（register 不在逐帧 dispatch 处）
        d = docs[0]
        assert d["generation_id"].startswith("gen_")
        assert d["status"] == "completed"
        assert d["finish_reason"] == "stop"
        assert d["session_id"] == "default"
        assert d["message_id"].startswith("msg_")  # assistant 父消息 ID 预分配贯通
        assert d["model"] == "mock"
        assert d["created_at"] and d["finished_at"]
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_register_uses_resolved_target_session(self):
        """conversation_id 解析的目标会话写 generation.session_id（D-m 归属）。"""
        conv_x = SessionMemory(session_id="conv_x")
        reg = _registry()
        loop = _make_loop({"conv_x": conv_x}, SessionMemory(session_id="default"), reg)
        await loop.process("hello", conversation_id="conv_x", protocol_version=2)
        await _await_pending(loop)
        docs_x = await _session_docs(reg, "conv_x")
        docs_default = await _session_docs(reg, "default")
        assert len(docs_x) == 1
        assert len(docs_default) == 0
        assert docs_x[0]["session_id"] == "conv_x"
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_error_path_marks_failed(self):
        async def faulty_stream(messages, tools=None, **kw):
            req = object()
            resp = type("R", (), {"status_code": 503})()
            raise httpx.HTTPStatusError("unavailable", request=req, response=resp)
            yield  # pragma: no cover —— yield 使其为 async generator（async for 可迭代）

        reg = _registry()
        loop = _make_loop({}, SessionMemory(session_id="default"), reg,
                          stream_impl=faulty_stream)
        await loop.process("hi")
        await _await_pending(loop)
        docs = await _session_docs(reg, "default")
        assert len(docs) == 1
        d = docs[0]
        assert d["status"] == "failed"      # 错误路径终态
        assert d["finish_reason"] == "error"
        assert d["error"]                    # error 信息已落
        assert d["finished_at"]              # 终态
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_interrupt_disconnect_marks_interrupted(self):
        started = asyncio.Event()
        blocker = asyncio.Event()

        async def blocking_stream(messages, tools=None, **kw):
            started.set()
            await blocker.wait()
            yield  # pragma: no cover —— yield 使其为 async generator（async for 可迭代）

        reg = _registry()
        loop = _make_loop({}, SessionMemory(session_id="default"), reg,
                          stream_impl=blocking_stream)
        task = asyncio.create_task(loop.process("hi"))
        await started.wait()
        loop.cancel_current("disconnect")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        await _await_pending(loop)
        docs = await _session_docs(reg, "default")
        assert len(docs) == 1
        assert docs[0]["status"] == "interrupted"  # disconnect → interrupted
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_interrupt_vad_marks_cancelled(self):
        started = asyncio.Event()
        blocker = asyncio.Event()

        async def blocking_stream(messages, tools=None, **kw):
            started.set()
            await blocker.wait()
            yield  # pragma: no cover —— yield 使其为 async generator（async for 可迭代）

        reg = _registry()
        loop = _make_loop({}, SessionMemory(session_id="default"), reg,
                          stream_impl=blocking_stream)
        task = asyncio.create_task(loop.process("hi"))
        await started.wait()
        loop.cancel_current("vad")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        await _await_pending(loop)
        docs = await _session_docs(reg, "default")
        assert len(docs) == 1
        assert docs[0]["status"] == "cancelled"  # vad/button → cancelled
        await loop._output.close()
