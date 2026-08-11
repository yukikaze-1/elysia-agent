"""FakeLLM / with_deadline / CountingLLM 确定性 fake-LLM 基建测试。"""

import asyncio
import json

import pytest

from Module.workflow_engine.tests.helpers.fake_llm import (
    CountingLLM,
    FakeLLM,
    with_deadline,
)

pytestmark = pytest.mark.unit


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
            self.calls = []
            self.chat = self          # 属性自引用（非方法）——委托链 real.chat.completions.create 走属性
            self.completions = self
        async def create(self, *, messages, **kwargs):
            self.calls.append(messages); return "ok"
    real = Real()
    cl = CountingLLM(real)
    await cl.create(messages=[{"role": "user", "content": "x"}], model="m")
    assert cl.count == 1
    assert real.calls == [[{"role": "user", "content": "x"}]]   # messages 被透传
