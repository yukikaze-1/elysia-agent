"""批次 5 Task 8：generation.model 快照捕获（D8）——loop turn 流程（marker=service）。

覆盖 loop 侧接线：
- turn 启动构造 GenerationContext 传 self._llm.model（快照捕获，替代 completed 即时读取）
- turn_started → v2 generation.started.model == llm.model
- turn_completed → generation.completed.model 与 Started 一致（同一次快照）
- failed 事件 → generation.failed 帧含 model（快照）
- GenerationRegistry 仍存 model（零改动回归确认）

使用 v2 WebSocketChannel（AsyncMock ws）记录 wire 帧 + 内存 FakeDocumentDBBackend（不碰真实 Mongo）。
"""

import asyncio

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.status.agent_fsm import AgentFSM
from core.monitoring.event_bus import EventBus
from Module.output import OutputManager
from Module.output.websocket import WebSocketChannel
from core.memory.session import SessionMemory
from core.cognitive import CognitiveLoop
from core.cognitive.generation_registry import GenerationRegistry
from core.cognitive.tests.conftest import make_default_registry
from core.storage.generation_repo import GenerationRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.adapter.llm_adapter import LLMAdapter

pytestmark = pytest.mark.service

_MODEL = "fake/test"


def _make_loop(stream_impl=None):
    """构造最小 CognitiveLoop + v2 WebSocketChannel（AsyncMock ws 记录 wire 帧）。

    Returns:
        (loop, ws, registry)：loop 供 process；ws 供帧断言；registry 供落库断言。
    """
    ws = AsyncMock()
    llm = MagicMock(spec=LLMAdapter)
    llm.model = _MODEL
    if stream_impl is not None:
        llm.stream_think = stream_impl
    else:
        async def fake_stream(messages, tools=None, **kw):
            for c in "你好":
                yield {"content": c, "tool_calls": None}
        llm.stream_think = fake_stream

    output = OutputManager()
    output.add_channel(WebSocketChannel(ws, protocol_version=2))
    fsm = AgentFSM()
    bus = EventBus()
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    prompt.build_messages = AsyncMock(return_value=[
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ])
    registry = GenerationRegistry(repo=GenerationRepository(db=FakeDocumentDBBackend()))
    default_session = SessionMemory(session_id="default")

    async def resolver(conversation_id, protocol_version):
        return default_session

    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt, session_resolver=resolver,
        generation_registry=registry,
        runtime_registry=make_default_registry(),
    )
    return loop, ws, registry


async def _await_pending(loop: CognitiveLoop) -> None:
    """等待所有 pending after_response task 完成。"""
    pending = [t for t in loop._pending_after_response if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _frames(ws) -> list[dict]:
    return [c.args[0] for c in ws.send_json.await_args_list]


class TestLoopGenerationModelSnapshot:
    @pytest.mark.asyncio
    async def test_started_and_completed_share_snapshot_model(self):
        """Started.model 与 Completed.model 一致（同一次快照，非即时读取）。"""
        loop, ws, reg = _make_loop()
        await loop.process("hello")
        await _await_pending(loop)
        frames = await _frames(ws)
        started = next(f for f in frames if f["type"] == "generation.started")
        completed = next(f for f in frames if f["type"] == "generation.completed")
        assert started["model"] == _MODEL          # Started 填 llm.model 快照
        assert completed["model"] == _MODEL        # Completed 用同快照
        assert completed["model"] == started["model"]
        # GenerationRegistry 零改动回归：仍存 model
        docs, _, _ = await reg.query_by_session("default")
        assert docs and docs[-1]["model"] == _MODEL
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_failed_frame_carries_snapshot_model(self):
        """错误路径：generation.failed 帧含 model（快照）。"""
        async def faulty_stream(messages, tools=None, **kw):
            req = object()
            resp = type("R", (), {"status_code": 503})()
            raise httpx.HTTPStatusError("unavailable", request=req, response=resp)
            yield  # pragma: no cover —— yield 使其为 async generator（async for 可迭代）

        loop, ws, reg = _make_loop(stream_impl=faulty_stream)
        await loop.process("hi")
        await _await_pending(loop)
        frames = await _frames(ws)
        failed = next(f for f in frames if f["type"] == "generation.failed")
        assert failed["model"] == _MODEL
        # 错误路径终态仍落 failed（registry 回归）
        docs, _, _ = await reg.query_by_session("default")
        assert docs and docs[-1]["status"] == "failed"
        await loop._output.close()
