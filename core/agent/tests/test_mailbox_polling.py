"""Agent._poll_mailboxes 意外错误日志测试 — S2。

原实现外层 `except Exception: pass` 无日志，注释称「get_nowait 抛异常 = 队列空」，
但 asyncio.QueueEmpty 已在内部显式处理——外层实际吞掉的是事件处理真实错误
（event.data 访问 / TaskResultData 构造 / bus.post）。
"""
import asyncio
import logging
import types

import pytest

from core.agent.agent import Agent, RunState
from core.agent._types import _BackgroundTaskEntry


class _StubMailbox:
    """get_nowait 抛非 QueueEmpty 意外错误的 mailbox。"""

    def get_nowait(self):
        raise RuntimeError("corrupt mailbox")


class _StubBus:
    def __init__(self):
        self.posted = []

    def post(self, *args, **kwargs):
        self.posted.append((args, kwargs))


class _CancelledMailbox:
    """get_nowait 返回一个 CANCELLED 终态事件的 mailbox。"""

    def __init__(self):
        self._yielded = False

    def get_nowait(self):
        if self._yielded:
            raise asyncio.QueueEmpty
        self._yielded = True
        return type("Ev", (), {
            "type": type("T", (), {"value": "cancelled"})(),
            "data": {"reason": "用户取消"},
        })()


@pytest.mark.service
@pytest.mark.asyncio
async def test_poll_mailboxes_logs_warning_on_event_error(caplog):
    """mailbox 轮询遇意外错误 → warning 日志而非静默吞掉。"""
    stub = object.__new__(Agent)
    stub._state = RunState.RUNNING
    stub._background_tasks = {
        "t1": _BackgroundTaskEntry(
            intent="测试任务",
            dispatched_at="2026-01-01T00:00:00Z",
            mailbox=_StubMailbox(),
        ),
    }
    stub._bus = _StubBus()
    stub._poll_mailboxes = types.MethodType(Agent._poll_mailboxes, stub)

    with caplog.at_level(logging.WARNING, logger="core.agent.agent"):
        task = asyncio.create_task(stub._poll_mailboxes())
        await asyncio.sleep(0.05)
        stub._state = RunState.STOPPED
        await asyncio.wait_for(task, timeout=3.0)

    assert any("mailbox" in r.message.lower() for r in caplog.records), (
        "mailbox 轮询意外错误必须留下 warning 日志"
    )


@pytest.mark.service
@pytest.mark.asyncio
async def test_poll_mailboxes_consumes_cancelled_event():
    """CANCELLED 终态事件 → pop _background_tasks 条目 + 呈现任务结果（E3 跨层缺口回归）。"""
    stub = object.__new__(Agent)
    stub._state = RunState.RUNNING
    stub._background_tasks = {
        "t1": _BackgroundTaskEntry(
            intent="测试任务",
            dispatched_at="2026-01-01T00:00:00Z",
            mailbox=_CancelledMailbox(),
        ),
    }
    bus = _StubBus()
    stub._bus = bus
    stub._poll_mailboxes = types.MethodType(Agent._poll_mailboxes, stub)

    task = asyncio.create_task(stub._poll_mailboxes())
    await asyncio.sleep(0.05)          # 轮询至少跑一轮
    stub._state = RunState.STOPPED
    await asyncio.wait_for(task, timeout=3.0)

    assert "t1" not in stub._background_tasks        # 条目已 pop（不永久驻留）
    assert len(bus.posted) == 1                       # 呈现了一条任务结果
    payload = bus.posted[0][0][0]
    assert "已取消" in payload.result
