"""WorkflowMailbox 单元测试。"""

import asyncio

import pytest

from Module.workflow_engine.mailbox import WorkflowMailbox
from Module.workflow_engine.schema import WorkflowEvent, WorkflowEventType, WorkflowId

pytestmark = pytest.mark.unit


def test_get_nowait_empty():
    """空队列 get_nowait 返回 None。"""
    mb = WorkflowMailbox(WorkflowId("t1"))
    assert mb.get_nowait() is None
    assert mb.count() == 0


def test_push_and_get_nowait():
    """push 后 get_nowait 能取出。"""
    mb = WorkflowMailbox(WorkflowId("t1"))
    mb._push(WorkflowEvent(type=WorkflowEventType.PROGRESS, data={"n": 1}))
    mb._push(WorkflowEvent(type=WorkflowEventType.COMPLETED, data={"result": "ok"}))

    assert mb.count() == 2
    e1 = mb.get_nowait()
    assert e1 is not None
    assert e1.type == WorkflowEventType.PROGRESS
    assert e1.data["n"] == 1

    e2 = mb.get_nowait()
    assert e2 is not None
    assert e2.type == WorkflowEventType.COMPLETED

    assert mb.get_nowait() is None
    assert mb.count() == 0


@pytest.mark.asyncio
async def test_get_blocking():
    """get() 在有事件时立即返回，无事件时等待。"""
    mb = WorkflowMailbox(WorkflowId("t1"))

    mb._push(WorkflowEvent(type=WorkflowEventType.PROGRESS, data={"n": 1}))
    e = await mb.get(timeout=0.1)
    assert e is not None
    assert e.type == WorkflowEventType.PROGRESS

    # 无事件应超时
    e = await mb.get(timeout=0.05)
    assert e is None


@pytest.mark.asyncio
async def test_get_wakes_on_push():
    """get() 阻塞等待时，push 能唤醒它。"""
    mb = WorkflowMailbox(WorkflowId("t1"))

    async def delayed_push():
        await asyncio.sleep(0.05)
        mb._push(WorkflowEvent(type=WorkflowEventType.COMPLETED, data={"result": "done"}))

    asyncio.create_task(delayed_push())
    e = await mb.get(timeout=1.0)
    assert e is not None
    assert e.type == WorkflowEventType.COMPLETED
    assert e.data["result"] == "done"


def test_overflow_drops_oldest_progress():
    """队列满时，新事件（非 progress）踢掉最旧的 progress。"""
    mb = WorkflowMailbox(WorkflowId("t1"))

    # 放满 progress
    for i in range(100):
        mb._push(WorkflowEvent(type=WorkflowEventType.PROGRESS, data={"n": i}))

    assert mb.count() == 100

    # 再放一个 completed，应踢掉最旧的 progress (n=0)
    mb._push(WorkflowEvent(type=WorkflowEventType.COMPLETED, data={"result": "ok"}))

    assert mb.count() == 100
    first = mb.get_nowait()
    # n=0 被踢，n=1 还在
    assert isinstance(first, WorkflowEvent)
    assert first.data["n"] == 1


def test_overflow_all_critical_keeps_new():
    """队列满且全是关键事件时，新关键事件踢掉最旧的。"""
    mb = WorkflowMailbox(WorkflowId("t1"))

    # 放满 completed
    for i in range(100):
        mb._push(WorkflowEvent(type=WorkflowEventType.COMPLETED, data={"n": i}))

    assert mb.count() == 100

    # 再放一个新的 completed，应踢掉最旧的 (n=0)
    mb._push(WorkflowEvent(type=WorkflowEventType.COMPLETED, data={"n": 100}))

    assert mb.count() == 100
    first = mb.get_nowait()
    assert isinstance(first, WorkflowEvent)
    assert first.data["n"] == 1  # n=0 被踢
    # n=100 在队尾
    last = None
    while True:
        e = mb.get_nowait()
        if e is None:
            break
        last = e
    assert isinstance(last, WorkflowEvent)
    assert last.data["n"] == 100


def test_overflow_all_critical_drops_new_progress():
    """队列满且全是关键事件时，新 progress 被静默丢弃。"""
    mb = WorkflowMailbox(WorkflowId("t1"))

    # 放满 completed
    for i in range(100):
        mb._push(WorkflowEvent(type=WorkflowEventType.COMPLETED, data={"n": i}))

    assert mb.count() == 100

    # 放一个新的 progress，应被丢弃
    mb._push(WorkflowEvent(type=WorkflowEventType.PROGRESS, data={"n": "should_drop"}))

    assert mb.count() == 100  # 不应增加
    # 确认全是 completed
    first = mb.get_nowait()
    assert isinstance(first, WorkflowEvent)
    assert first.type == WorkflowEventType.COMPLETED


def test_overflow_progress_is_discarded():
    """队列满且全是 progress 时，新 progress 被丢弃，队列不变。"""
    mb = WorkflowMailbox(WorkflowId("t1"))

    for i in range(100):
        mb._push(WorkflowEvent(type=WorkflowEventType.PROGRESS, data={"n": i}))

    mb._push(WorkflowEvent(type=WorkflowEventType.PROGRESS, data={"n": 100}))
    # n=0 被踢掉，n=100 进入队尾
    assert mb.count() == 100
    first = mb.get_nowait()
    assert isinstance(first, WorkflowEvent)
    assert first.data["n"] == 1


def test_all_event_types():
    """所有 6 种事件类型都能正常 push/get。"""
    mb = WorkflowMailbox(WorkflowId("t1"))

    for t in WorkflowEventType:
        mb._push(WorkflowEvent(type=t, data={"k": t.value}))

    assert mb.count() == len(WorkflowEventType)

    seen = set()
    while True:
        e = mb.get_nowait()
        if e is None:
            break
        seen.add(e.type)

    assert seen == set(WorkflowEventType)


@pytest.mark.asyncio
async def test_concurrent_get_multiple():
    """多个消费者？不应有——但验证单个消费者并发安全性。"""
    mb = WorkflowMailbox(WorkflowId("t1"))
    received: list[WorkflowEvent] = []

    async def consumer(n: int):
        for _ in range(n):
            e = await mb.get(timeout=1.0)
            if e is not None:
                received.append(e)

    # 生产者先放 3 个
    for i in range(3):
        mb._push(WorkflowEvent(type=WorkflowEventType.PROGRESS, data={"n": i}))

    c = asyncio.create_task(consumer(3))
    await asyncio.wait_for(c, timeout=1.0)
    assert len(received) == 3
    assert [e.data["n"] for e in received] == [0, 1, 2]
