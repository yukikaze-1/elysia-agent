# 测试文件：core/monitoring/tests/test_event_bus_ownership.py
"""Batch 14（KB-47）：`EventBus.discard_user_inputs()` 的**分类**语义。

停机边界要丢弃的是"非 durable、未被任何回合接管"的 accepted 输入；这一层是纯容器
语义，与 Agent 无关，因此单独在此钉住三件事：

1. **按 event type 分类**——只移除 USER_INPUT，durable 的 `TaskResultData` 原位保留
   （§34/§59 硬边界：不得退化成 `clear()`）；
2. **幸存者顺序与优先级不变**（重投不得打乱 `(priority, seq)` 的顺序）；
3. **计数/快照簿记与队列实况一致**（`pending_user_inputs` /
   `pending_user_input_request_ids()` 在丢弃后必须归零，不能只减队列不清快照）。
"""

from __future__ import annotations

import pytest

from core.monitoring.event_bus import EventBus, EventPriority
from core.schema.event import StateChangeData, TaskResultData, UserInputData
from core.schema.agent import AgentState, EventType
from core.schema.types import RequestId

pytestmark = pytest.mark.unit


def _user(text: str, rid: str) -> UserInputData:
    return UserInputData(text=text, user_name="u", request_id=RequestId(rid))


def _task(task_id: str) -> TaskResultData:
    return TaskResultData(task_id=task_id, result="r", original_intent="i",
                          dispatched_at="2026-09-19T00:00:00Z")


def _state(name: AgentState) -> StateChangeData:
    return StateChangeData(state=name, request_id=RequestId("001"))


def test_discards_only_user_input_and_keeps_durable_events():
    """分类：只丢 USER_INPUT；durable 终态与状态帧原位保留（不得全清）。"""
    bus = EventBus()
    bus.post(_user("M1", "001"))
    bus.post(_task("t-1"))
    bus.post(_user("M2", "002"))
    bus.post(_state(AgentState.IDLE), priority=EventPriority.STATE_CHANGE)
    assert bus.pending_user_inputs == 2
    assert bus.pending_count == 4

    dropped = bus.discard_user_inputs()

    assert [ev.data.text for ev in dropped] == ["M1", "M2"]
    assert bus.pending_user_inputs == 0
    assert bus.pending_user_input_request_ids() == ()
    assert bus.pending_count == 2, "durable 终态/状态帧不得被一起清掉"
    survivors = [(ev.priority, ev.type) for ev in bus.drain()]
    # 出队顺序 = `(priority, seq)`：STATE_CHANGE(1) 先于 NORMAL(2)。
    assert survivors == [
        (EventPriority.STATE_CHANGE, EventType.STATE_CHANGE),
        (EventPriority.NORMAL, EventType.TASK_RESULT),
    ]


def test_survivor_order_is_preserved():
    """重投幸存者不得改变出队顺序——与"从未出现过 USER_INPUT"的对照总线逐项相同。"""
    reference = EventBus()
    reference.post(_task("t-1"))
    reference.post(_state(AgentState.THINKING), priority=EventPriority.STATE_CHANGE)
    reference.post(_task("t-2"))
    reference.post(_state(AgentState.IDLE), priority=EventPriority.STATE_CHANGE)

    bus = EventBus()
    bus.post(_task("t-1"))
    bus.post(_state(AgentState.THINKING), priority=EventPriority.STATE_CHANGE)
    bus.post(_user("M1", "001"))
    bus.post(_task("t-2"))
    bus.post(_state(AgentState.IDLE), priority=EventPriority.STATE_CHANGE)

    bus.discard_user_inputs()

    def _order(b: EventBus) -> list[object]:
        return [getattr(ev.data, "task_id", None) or ev.data.state.value
                for ev in b.drain()]

    assert _order(bus) == _order(reference) == ["thinking", "idle", "t-1", "t-2"]


def test_discard_is_empty_when_no_user_input():
    """无 USER_INPUT 时是 no-op（不产生副作用、不改动队列）。"""
    bus = EventBus()
    bus.post(_task("t-1"))
    assert bus.discard_user_inputs() == []
    assert bus.pending_count == 1
    assert bus.pending_user_inputs == 0


def test_dispatched_user_input_is_not_affected():
    """已经被取走（派发）的事件不在队列里，本方法看不见它——它属于 active turn。"""
    bus = EventBus()
    bus.post(_user("M1", "001"))
    bus.post(_user("M2", "002"))

    first = bus._queue.get_nowait()          # noqa: SLF001 — 模拟消费者已取走首条
    bus._note_removed(first)                 # noqa: SLF001

    dropped = bus.discard_user_inputs()
    assert [ev.data.text for ev in dropped] == ["M2"]
    assert bus.pending_count == 0
