"""WorkflowMailbox 单元测试。"""

import asyncio

import pytest

from Module.workflow_engine.mailbox import WorkflowMailbox, push_mailbox_event
from Module.workflow_contract import (
    WorkflowEvent,
    WorkflowEventType,
    WorkflowId,
)

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


# ── 终态事件唯一性守卫（E3 Task 1）────────────────────────────────────────


class TestMailboxTerminalGuard:
    def test_terminal_event_once(self):
        mb = WorkflowMailbox(WorkflowId("t1"))
        push_mailbox_event(mb, WorkflowEventType.COMPLETED, {})
        push_mailbox_event(mb, WorkflowEventType.CANCELLED, {})
        assert mb.count() == 1          # 第二个终态被守卫丢弃


# ── 终态同步落盘 sink（#6 硬杀窗口）─────────────────────────────────────────


class TestTerminalSink:
    """`push_mailbox_event` 的终态落盘回调语义。

    sink 由 core 经 `AgentInfra.terminal_result_sink` 提供、engine 在 `submit()` 时
    挂到 mailbox 上（挂载点在 `test_workflow_engine.py::TestTerminalSinkAttachment`）。
    这里只管"什么事件调它、什么事件不调它、它抛了会怎样"。
    """

    def test_terminal_events_call_sink_with_raw_data(self):
        calls: list[tuple] = []
        mb = WorkflowMailbox(WorkflowId("t1"))
        mb.terminal_sink = lambda tid, event, data: calls.append((tid, event, data))

        push_mailbox_event(mb, WorkflowEventType.FAILED, {"error": "崩了"})

        assert calls == [("t1", "failed", {"error": "崩了"})], \
            "传原始 data（措辞是 core 的事——两条投递路径必须给出同一文本）"

    def test_non_terminal_events_do_not_call_sink(self):
        calls: list[tuple] = []
        mb = WorkflowMailbox(WorkflowId("t1"))
        mb.terminal_sink = lambda tid, event, data: calls.append((tid, event, data))

        push_mailbox_event(mb, WorkflowEventType.QUEUED, {"task_id": "t1"})
        push_mailbox_event(mb, WorkflowEventType.PROGRESS, {"step": "1/3"})
        push_mailbox_event(mb, WorkflowEventType.PAUSED, {})

        assert calls == [], "非终态落盘等于每条 progress 一次 fsync"

    def test_duplicate_terminal_does_not_reach_sink(self):
        calls: list[tuple] = []
        mb = WorkflowMailbox(WorkflowId("t1"))
        mb.terminal_sink = lambda tid, event, data: calls.append((tid, event, data))

        push_mailbox_event(mb, WorkflowEventType.COMPLETED, {"result": "先到"})
        push_mailbox_event(mb, WorkflowEventType.FAILED, {"error": "重复终态"})

        assert [c[1] for c in calls] == ["completed"], \
            "被终态唯一性守卫丢弃的重复终态不该进 WAL（否则启动补投会给出两个结果）"

    def test_unwired_mailbox_is_noop(self):
        mb = WorkflowMailbox(WorkflowId("t1"))
        assert mb.terminal_sink is None, "默认不落盘（单测/未接线）"
        push_mailbox_event(mb, WorkflowEventType.COMPLETED, {"result": "x"})
        assert mb.count() == 1, "未接线不影响事件入队"

    def test_sink_raising_does_not_break_push(self):
        def _boom(tid, event, data):  # noqa: ANN001, ANN202
            raise RuntimeError("磁盘满了")

        mb = WorkflowMailbox(WorkflowId("t1"))
        mb.terminal_sink = _boom

        push_mailbox_event(mb, WorkflowEventType.COMPLETED, {"result": "x"})

        assert mb.count() == 1, "落盘失败绝不能把任务终态推送弄挂"

    def test_each_mailbox_holds_its_own_sink(self):
        """sink 随 mailbox 生命周期——一个任务一个，不会跨任务串（task_id 由 mailbox 提供）。"""
        seen: list[str] = []
        sinks = []
        for i in range(2):
            mb = WorkflowMailbox(WorkflowId(f"t{i}"))
            mb.terminal_sink = (lambda tid, event, data: seen.append(tid))
            sinks.append(mb)
        for mb in sinks:
            push_mailbox_event(mb, WorkflowEventType.COMPLETED, {})
        assert seen == ["t0", "t1"]
