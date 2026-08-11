"""FakeLLM / with_deadline / CountingLLM 确定性 fake-LLM 基建测试。"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from Module.workflow_contract import (
    AgentInfra,
    ConstraintConfig,
    DelegationContext,
    SessionId,
    WorkflowEventType,
)
from Module.workflow_engine.mailbox import WorkflowMailbox
from Module.workflow_engine.tests.helpers.fake_llm import (
    CountingLLM,
    FakeLLM,
    with_deadline,
)
from Module.workflow_engine.workflow_engine import WorkflowEngine


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fake_llm_text_direct():
    fake = FakeLLM()
    fake.handler = lambda msgs: {"type": "text", "content": "hi"}
    resp = await fake.create(messages=[{"role": "user", "content": "x"}])
    assert resp.choices[0].message.content == "hi"
    assert resp.choices[0].message.tool_calls is None
    assert resp.usage.prompt_tokens == 1          # 属性访问
    assert fake.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fake_llm_tool_match():
    fake = FakeLLM()
    fake.handler = lambda msgs: {"type": "tool", "name": "get_time", "arguments": {"tz": "UTC"}}
    resp = await fake.create(messages=[])
    tc = resp.choices[0].message.tool_calls[0]
    assert tc.function.name == "get_time"
    assert json.loads(tc.function.arguments) == {"tz": "UTC"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fake_llm_gate_blocks_until_released():
    fake = FakeLLM()
    fake.gate = asyncio.Event()
    task = asyncio.create_task(fake.create(messages=[]))
    await asyncio.sleep(0.02)
    assert fake.calls == 0                        # gate 未放行，未计数
    fake.gate.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert fake.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_with_deadline_times_out():
    with pytest.raises(asyncio.TimeoutError):
        await with_deadline(asyncio.sleep(5), timeout=0.05)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_counting_llm_forwards_messages():
    class Real:
        def __init__(self):
            self.calls = []          # (messages, kwargs) 记录——断言 messages 与 kwargs 均透传
            self.chat = self          # 属性自引用（非方法）——委托链 real.chat.completions.create 走属性
            self.completions = self
        async def create(self, *, messages, **kwargs):
            self.calls.append((messages, kwargs)); return "ok"
    real = Real()
    cl = CountingLLM(real)
    await cl.create(messages=[{"role": "user", "content": "x"}], model="m", temperature=0.5)
    assert cl.count == 1
    assert real.calls == [([{"role": "user", "content": "x"}], {"model": "m", "temperature": 0.5})]   # messages + kwargs 均透传


@pytest.mark.service
@pytest.mark.asyncio
async def test_submit_first_event_is_queued():
    """submit 立即返回 mailbox，首事件为 QUEUED（E2 契约）。"""
    fake = FakeLLM()
    infra = AgentInfra(llm=fake, milvus=None, http=None,
                       tool_registry=MagicMock(), constraints=ConstraintConfig())
    engine = WorkflowEngine(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=1)
    await engine.start()
    # 占满槽 → 新任务排队。注：asyncio.BoundedSemaphore.acquire_nowait() 为 3.13+，
    # 项目为 3.12——用 await acquire()（队列空时 runner 停在 queue.get()，必能拿到 permit）。
    await engine._semaphore.acquire()
    try:
        ctx = DelegationContext(intent="t", session_id=SessionId("s1"))
        result = await with_deadline(engine.submit(ctx), timeout=1.0)
        assert isinstance(result, WorkflowMailbox)
        ev = await with_deadline(result.get(), timeout=1.0)   # 首事件
        assert ev.type == WorkflowEventType.QUEUED
    finally:
        engine._semaphore.release()             # 释放预占 permit——防失败路径遗留 worker 阻塞（loop 关闭告警）
        await engine.stop()
