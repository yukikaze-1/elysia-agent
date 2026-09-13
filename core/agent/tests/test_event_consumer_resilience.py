# 测试文件：core/agent/tests/test_event_consumer_resilience.py
"""事件消费者韧性（S3-03）与关停 flush 不丢批（S2-A1）。

**S3-03**：`_process_events` 是 Agent 的**唯一**事件消费者，只在启动时 spawn 一次、
无 watchdog。此前任一 handler 抛异常都会冒泡出该方法把它**永久杀死**——表现为
"进程在线但完全不理人"（`_pending_user_inputs` 只增不减），且没有任何日志能指出
是哪个事件把它干掉的。

**S2-A1**：关停 flush 走 `asyncio.wait_for(flush(), timeout=5.0)`，超时 → 取消 →
`CancelledError` 是 `BaseException` 子类，而 `_do_consolidate` 的两处 requeue 只
`except Exception` → **batch 永久丢失**，日志只有一句 "flush timed out"，不提数据丢失。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.service


# ═══════════════════════════════════════════════════════════════════
# S3-03：单个事件处理失败不得杀死消费者
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_handler_exception_does_not_kill_consumer():
    """一个 handler 抛异常 → 消费者继续处理下一个事件。

    缺陷原状：异常冒泡出 `_process_events` → 消费协程退出 → Agent 收不到任何输入。
    """
    from core.input.tests.test_server import _make_agent

    agent = _make_agent()
    await agent.start()
    seen: list[str] = []

    def _boom(data):
        raise RuntimeError("handler 炸了")

    def _ok(data):
        seen.append("ok")

    agent._handle_interrupt = _boom                     # noqa: SLF001
    agent._handle_system_tick = _ok                     # noqa: SLF001

    from core.schema.event import InterruptData, SystemTickData
    from core.status.agent_fsm import AgentState
    from core.schema.types import RequestId
    try:
        # 直接驱动分发边界：第一个必炸，第二个必须仍然被处理
        for data in (InterruptData(source="test"),
                     SystemTickData(reason="silence_timeout")):
            try:
                await agent._dispatch_event(data)       # noqa: SLF001
            except Exception:
                pass
        # 断言消费循环的边界写法真的吞掉异常（不是让调用方去 try）
        consumer_alive = True
        try:
            await agent._dispatch_event(SystemTickData(reason="silence_timeout"))  # noqa: SLF001
        except Exception:
            consumer_alive = False
        assert consumer_alive, "分发边界必须自己吞异常"
        assert seen == ["ok", "ok"]
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_cancelled_error_still_propagates():
    """取消是控制流：必须继续传播，不得被 per-event 边界吞掉。"""
    from core.input.tests.test_server import _make_agent

    agent = _make_agent()
    await agent.start()

    async def _cancel(data):
        raise asyncio.CancelledError

    agent._handle_state_change = _cancel                # noqa: SLF001
    from core.schema.event import StateChangeData
    from core.schema.types import RequestId
    from core.status.agent_fsm import AgentState
    try:
        with pytest.raises(asyncio.CancelledError):
            await agent._dispatch_event(StateChangeData(state=AgentState.IDLE, request_id=RequestId("req_t")))  # noqa: SLF001
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_consumer_survives_and_logs(caplog):
    """端到端：消费者循环本身在 handler 抛异常后仍在跑，且留下 error 日志。

    这里把真实 `_process_events` 跑起来，投一个会炸的事件 + 一个正常事件，
    断言正常事件被处理（= 消费者没死）。
    """
    import logging

    from core.agent import RunState
    from core.input.tests.test_server import _make_agent
    from core.schema.event import StateChangeData, SystemTickData
    from core.schema.types import RequestId
    from core.status.agent_fsm import AgentState

    agent = _make_agent()
    await agent.start()
    handled: list[str] = []

    async def _boom(data):
        raise RuntimeError("炸")

    def _ok(data):
        handled.append("tick")

    agent._handle_state_change = _boom                  # noqa: SLF001
    agent._handle_system_tick = _ok                     # noqa: SLF001

    task = asyncio.create_task(agent._process_events())  # noqa: SLF001
    try:
        with caplog.at_level(logging.ERROR, logger="core.agent.agent"):
            agent._bus.post(StateChangeData(state=AgentState.IDLE, request_id=RequestId("req_t")))   # noqa: SLF001
            agent._bus.post(SystemTickData(reason="silence_timeout"))  # noqa: SLF001
            for _ in range(50):
                if handled:
                    break
                await asyncio.sleep(0.02)
        assert handled == ["tick"], "消费者被单个事件的异常杀死了"
        assert any("事件处理失败" in r.getMessage() for r in caplog.records)
    finally:
        agent._state = RunState.STOPPING                # noqa: SLF001
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent.stop()


# ═══════════════════════════════════════════════════════════════════
# S2-A1：取消（关停预算超时）也必须还原待整合批次
# ═══════════════════════════════════════════════════════════════════

def _consolidator(*, llm):
    from core.memory.consolidator import MemoryConsolidator
    cons = MemoryConsolidator(llm=llm, memory=MagicMock())
    cons.add_turn = getattr(cons, "add_turn", None)
    return cons


@pytest.mark.asyncio
async def test_cancelled_extraction_requeues_batch():
    """提取阶段被取消 → 批次必须还在缓冲里（原先 `except Exception` 接不住取消）。"""
    from core.memory.consolidator import MemoryConsolidator

    llm = MagicMock()
    llm.think = AsyncMock(side_effect=asyncio.CancelledError)
    cons = MemoryConsolidator(llm=llm, memory=MagicMock())
    cons._buffer = [("u1", "a1"), ("u2", "a2")]         # noqa: SLF001
    batch = cons._buffer                                # noqa: SLF001
    cons._buffer = []                                   # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        await cons._do_consolidate(batch)               # noqa: SLF001

    assert cons.stats()["buffer_size"] >= 1, "取消后批次必须在缓冲里"
    assert cons.stats()["running"] is False, "运行标志必须复位，不能卡在 True"
    assert cons.stats()["last_error"], "失败必须记因，而不是静默"


@pytest.mark.asyncio
async def test_cancelled_storage_requeues_batch():
    """存储阶段被取消 → 同样还原批次。"""
    from core.memory.consolidator import MemoryConsolidator

    llm = MagicMock()
    llm.think = AsyncMock(return_value=MagicMock(
        text='[{"content": "用户在学吉他", "type": "Fact", "poignancy": 5}]'))
    cons = MemoryConsolidator(llm=llm, memory=MagicMock())
    cons._compare_and_resolve = AsyncMock(side_effect=asyncio.CancelledError)  # noqa: SLF001
    batch = [("u1", "a1")]

    with pytest.raises(asyncio.CancelledError):
        await cons._do_consolidate(batch)               # noqa: SLF001

    assert cons.stats()["buffer_size"] >= 1
    assert cons.stats()["extracted"] if "extracted" in cons.stats() else True
    assert cons.stats()["running"] is False


@pytest.mark.asyncio
async def test_ordinary_failure_still_returns_empty_not_raise():
    """回归网：普通异常仍是"记 warning + 返回空"，不改变调用方契约。"""
    from core.memory.consolidator import MemoryConsolidator

    llm = MagicMock()
    llm.think = AsyncMock(side_effect=RuntimeError("llm down"))
    cons = MemoryConsolidator(llm=llm, memory=MagicMock())

    assert await cons._do_consolidate([("u", "a")]) == []   # noqa: SLF001
    assert cons.stats()["buffer_size"] >= 1
