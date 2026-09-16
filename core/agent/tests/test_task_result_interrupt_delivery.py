# 测试文件：core/agent/tests/test_task_result_interrupt_delivery.py
"""Issue B：INTERRUPT 排空 EventBus 时，后台任务终态结果不得被静默、永久地丢掉。

**原缺陷有两半，各自独立成立**（本文件逐半钉死）：

1. **投递确认发生在入队时**（`core/agent/_lifecycle_task_system.py::drain_mailboxes_once`）：
   `delivered=True` 的判据是 "`_state == RUNNING`（消费者协程活着）"，而
   `_persist_task_result` 落库成功后**立刻清掉 WAL 副本**。于是 `bus.post` 返回的
   那一刻，这条结果在进程内仅剩的形态是**队列里一个没人认领的元素**，而库里已经
   写着"已投递"。那个判据是假设，不是不变量——没有任何东西取得过它的所有权。
2. **`_handle_interrupt` 清空队列**（`core/agent/agent.py`）：`self._bus.drain()`
   之后只把 `StateChangeData` 重投回去，`TaskResultData` 被直接丢弃。

Half 1 单独存在并不致命（结果还在队列里），Half 2 单独存在也不致命（库里还是
`delivered=False`，下次启动补投）。**两半叠加才是"永久且静默"**：EventBus 是
`(priority, seq)` 优先队列，`INTERRUPT = 0` 而 `TaskResultData` 走默认 `NORMAL = 2`
—— 中断不是"和结果赛跑"，而是**插到它前面**。消费者只要在任何一处 await
（LLM 流式、`mark_delivered` 的 Mongo 往返、`_handle_state_change`），
"终态入队 → 中断插队 → drain 掉它"就是可达序列，用户侧表现为：任务永远没有结果、
日志干净、库里写着 `delivered=True`。

**修复后的不变量**（`_lifecycle_task_system.py` 顶部的投递状态机注释是唯一权威）：

    WAL removed ∧ delivered=True ∧ ¬accepted   —— 禁止

即"WAL 副本已清 + 库说已投递 + 消费方从未取得所有权"三处皆无的状态不可达。
投递确认从**入队时**移到**消费时**（`_handle_task_result` 在 spawn 成功之后才
`mark_delivered`），中断 drain 对 `TaskResultData` 改为原位重投（搬移，不是复制）。

`_state == RUNNING` 现在只决定"排空节奏"（`_poll_mailboxes` 的循环条件），
**不再**决定任何持久化语义——这正是 Issue B 要拿掉的那条耦合。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from core.agent._types import RunState, _BackgroundTaskEntry
from core.agent.tests.test_task_result_durability import _StubRepo
from core.monitoring.event_bus import EventBus
from core.schema import AgentState, EventPriority, EventType
from core.schema.event import (
    InterruptData,
    StateChangeData,
    SystemTickData,
    TaskResultData,
    UserInputData,
)
from core.schema.types import RequestId, SessionId
from Module.workflow_contract import WorkflowEventType
from Module.workflow_engine.mailbox import WorkflowMailbox, push_mailbox_event

pytestmark = pytest.mark.service

_DISPATCHED_AT = "2026-09-15T00:00:00Z"


# ══════════════════════════════════════════════════════════════════
# 替身：真实 EventBus（优先级语义是缺陷的一半）+ 真实 Agent 方法
# ══════════════════════════════════════════════════════════════════

class _TracedBus(EventBus):
    """**真实** EventBus + post 时记一笔。

    刻意继承真实总线而不是手搓 `_StubBus`：本文件要验的正是"INTERRUPT(0) 越过
    NORMAL(2)"这条真实排序语义，替身队列会把缺陷一起替换掉（假绿）。
    """

    def __init__(self, trace: list[str]) -> None:
        super().__init__()
        self.trace = trace

    def post(self, data, priority=EventPriority.NORMAL, *, type=None) -> None:  # noqa: ANN001
        self.trace.append("post")
        super().post(data, priority=priority, type=type)


class _StubScope:
    """`AsyncTaskScope`（`agent._turns`）替身——记录宣告回合的 spawn 次数。"""

    def __init__(self, trace: list[str] | None = None) -> None:
        self.names: list[str] = []
        self.trace = trace

    def spawn(self, coro, *, name=None, log_errors=True):  # noqa: ANN001
        # `_cognitive.process` 是 MagicMock，返回的不是协程——不存在"未 await 的
        # coroutine"告警（pytest.ini 把那条 RuntimeWarning 置成 error）。
        self.names.append(name or "")
        if self.trace is not None:
            self.trace.append("spawn")
        return MagicMock(name=name)


class _StatefulRepo(_StubRepo):
    """在 `_StubRepo` 之上把 `list_pending` / `mark_delivered` 变成**真语义**。

    `_StubRepo` 的 `list_pending` 只回固定列表、`mark_delivered` 只记账不翻转记录，
    够不上"重启后从库里读回待补投记录"这类用例。这里让 `delivered` 成为库里记录的
    真实字段：`list_pending` 从写入记录派生，`mark_delivered` 翻转它。
    """

    async def mark_delivered(self, task_id) -> None:  # noqa: ANN001
        await super().mark_delivered(task_id)
        for row in self.written:
            if str(row["task_id"]) == str(task_id):
                row["delivered"] = True

    async def list_pending(self, **_kw) -> list[dict]:  # noqa: ANN003
        if self.fail_list:
            raise RuntimeError("mongo offline")
        return [dict(row) for row in self.written if not row.get("delivered", False)]


def _stub_agent(*, mailbox=None, repo=None, bus=None, trace=None, wal=None,
                intent: str = "写季度报告", task_id: str = "wf-1"):
    """最小 Agent 替身：真实 `_dispatch_event` / `_handle_interrupt` /
    `_handle_task_result` / `drain_mailboxes_once`，子系统为替身。

    `object.__new__(Agent)` 跳过 `__init__`，只补这些真实方法真正会碰到的字段。
    """
    from core.agent.agent import Agent

    agent = object.__new__(Agent)
    agent._state = RunState.RUNNING
    agent._pending_messages = []
    agent._req_counter = 0
    agent._background_tasks = {}
    if mailbox is not None:
        agent._background_tasks[task_id] = _BackgroundTaskEntry(
            intent=intent,
            dispatched_at=_DISPATCHED_AT,
            mailbox=mailbox,
        )
    agent._bus = bus if bus is not None else EventBus()
    agent._cognitive = MagicMock()      # cancel_current / process / current_task
    agent._turns = _StubScope(trace)
    agent._mailbox_task = None
    agent._lifecycle_tasks = MagicMock()
    agent._session_manager = MagicMock()
    agent._background_task_result_repo = repo
    agent._task_result_wal = wal
    return agent


async def _drain_mailboxes(agent) -> int:
    return await type(agent).drain_mailboxes_once(agent)


async def _redeliver(agent) -> None:
    await type(agent)._redeliver_pending_task_results(agent)


async def _next_event(agent, timeout: float = 1.0):
    """取下一个事件；队列空则**显式失败**而不是挂死（挂死看不出是哪一半坏了）。"""
    try:
        return await asyncio.wait_for(agent._bus.next(), timeout=timeout)
    except asyncio.TimeoutError:  # pragma: no cover - 失败路径
        return None


def _task_results(events) -> list[TaskResultData]:  # noqa: ANN001
    return [e.data for e in events if isinstance(e.data, TaskResultData)]


def _assert_not_forbidden(agent, repo, wal, *, stage: str) -> None:
    """禁止态守卫：`WAL 已清 ∧ delivered=True ∧ 消费方从未取得所有权`。"""
    wal_empty = wal.read_raw() == []
    acked = repo.delivered != []
    accepted = bool(agent._turns.names)
    assert not (wal_empty and acked and not accepted), (
        f"[{stage}] 出现禁止态：WAL 副本已清 + 库里 delivered=True + 消费方从未取得"
        "所有权——这条终态结果三处皆无（Issue B 的原缺陷）"
    )


# ══════════════════════════════════════════════════════════════════
# 0. 机制：钉住"INTERRUPT 越过已入队的 NORMAL 事件"这条排序事实
# ══════════════════════════════════════════════════════════════════

class TestPriorityOrderingFact:
    """缺陷可达性的**前提**：中断不是和结果赛跑，是插到它前面。

    它单独一条并不表示有缺陷（顺序本身是有意设计）。之所以要钉住：Issue B 的
    推理建立在"已入队的 `TaskResultData` 会被后到的 INTERRUPT 抢先"之上——
    若有人改了总线排序（例如把 INTERRUPT 降级成普通事件），下面所有用例的
    前提就变了，这条会红，提醒把推理重做一遍，而不是让旧结论静默失效。
    """

    @pytest.mark.asyncio
    async def test_interrupt_overtakes_already_queued_normal_event(self):
        bus = EventBus()
        task_result = TaskResultData(
            task_id="wf-1", result="报告写好了", original_intent="写季度报告",
            dispatched_at=_DISPATCHED_AT)
        bus.post(task_result)                     # NORMAL(2)，先入队
        bus.post(InterruptData(source="ws_disconnect"),
                 priority=EventPriority.INTERRUPT)  # INTERRUPT(0)，后入队

        first = await bus.next()

        assert isinstance(first.data, InterruptData), (
            "INTERRUPT 必须越过已入队的 NORMAL 事件（EventBus 的 (priority, seq) 排序）")
        assert first.priority == EventPriority.INTERRUPT
        second = await bus.next()
        assert second.data is task_result, "被越过的 NORMAL 事件必须仍在队列里（不得被插队者吃掉）"


# ══════════════════════════════════════════════════════════════════
# B1. 终态已入队但未被消费时来一条 INTERRUPT —— 结果不得蒸发
# ══════════════════════════════════════════════════════════════════

class TestInterruptKeepsQueuedTaskResult:
    @pytest.mark.asyncio
    async def test_interrupt_does_not_swallow_queued_terminal_result(self):
        """① 排空入队（无人认领）→ ② 中断插队排空 → ③ 结果仍必须被消费。

        修复前：`_handle_interrupt` 的 `drain()` 把 `TaskResultData` 直接扔掉，
        队列空、库里 `delivered=True`、WAL 已清 —— 三处皆无。
        """
        trace: list[str] = []
        repo = _StatefulRepo(trace=trace)
        bus = _TracedBus(trace)
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox=mailbox, repo=repo, bus=bus, trace=trace)
        push_mailbox_event(mailbox, WorkflowEventType.COMPLETED, {"result": "报告写好了"})

        # ① 终态结果入队（消费者还没取走）
        assert await _drain_mailboxes(agent) == 1
        assert bus.pending_count == 1

        # ② 中断插队 → 被 drain（这一步是缺陷现场）
        bus.post(InterruptData(source="ws_disconnect"),
                 priority=EventPriority.INTERRUPT)
        interrupt = await _next_event(agent)
        assert interrupt is not None and isinstance(interrupt.data, InterruptData)
        await agent._dispatch_event(interrupt.data)

        # ③ 结果必须还在（重投）——先证明队列里没丢
        survivors = _task_results(bus.drain())
        assert survivors, (
            "中断排空后，已入队但未被消费的终态结果**消失了**——"
            "它此刻在 WAL/库/队列三处都不在（Issue B 原缺陷）")
        assert len(survivors) == 1, f"重投必须是搬移不是复制，实得 {len(survivors)} 条"

        # ④ 而且它必须真的能被消费（spawn 出承载它的宣告回合）
        await agent._dispatch_event(survivors[0])
        assert agent._turns.names == ["task-result-wf-1"], (
            "重投后的终态结果必须经 `_handle_task_result` 唤醒认知循环")
        assert repo.delivered == ["wf-1"]


# ══════════════════════════════════════════════════════════════════
# B2. 混合 drain —— 逐类型策略必须显式且互不串味
# ══════════════════════════════════════════════════════════════════

class TestInterruptDrainPolicyPerType:
    @pytest.mark.asyncio
    async def test_mixed_queue_per_type_policy(self):
        """USER_INPUT + STATE_CHANGE + TASK_RESULT + SYSTEM_TICK 同时排队时中断。

        | 排队事件        | 策略                          |
        |-----------------|-------------------------------|
        | StateChangeData | 保留（重投）——UI 需要         |
        | UserInputData   | 释放会话 work 后丢弃          |
        | TaskResultData  | **重投**——持久化过的用户可见结果 |
        | 其他            | 丢弃                          |
        """
        bus = EventBus()
        agent = _stub_agent(bus=bus)
        task_result = TaskResultData(
            task_id="wf-1", result="报告写好了", original_intent="写季度报告",
            dispatched_at=_DISPATCHED_AT)
        user_input = UserInputData(
            text="你好", user_name="yomu", request_id=RequestId("req-u1"),
            conversation_id=SessionId("c1"), session_work_reserved=True)
        state_change = StateChangeData(state=AgentState.IDLE, request_id=RequestId("req-s1"))
        tick = SystemTickData(reason="time_of_day", elapsed=30)
        for data in (user_input, state_change, task_result, tick):
            bus.post(data)
        bus.post(InterruptData(source="user"), priority=EventPriority.INTERRUPT)

        interrupt = await _next_event(agent)
        assert interrupt is not None and isinstance(interrupt.data, InterruptData)
        await agent._dispatch_event(interrupt.data)

        agent._session_manager.release_work.assert_called_once_with("c1")
        agent._cognitive.cancel_current.assert_called_once_with(source="user")

        survivors = bus.drain()
        kinds = [type(e.data) for e in survivors]
        assert StateChangeData in kinds, "STATE_CHANGE 必须重投（前端 UI 依赖它）"
        assert TaskResultData in kinds, "TASK_RESULT 是持久化过的用户可见结果，不得丢弃"
        assert UserInputData not in kinds, "USER_INPUT 已释放 work → 丢弃（用户已取消）"
        assert SystemTickData not in kinds, "未登记类型保持既有行为：丢弃"
        assert len(survivors) == 2 and len(_task_results(survivors)) == 1
        reposted = next(e for e in survivors if isinstance(e.data, TaskResultData))
        assert reposted.priority == EventPriority.NORMAL, "重投必须**保留**原优先级（不升不降）"
        assert reposted.type == EventType.TASK_RESULT, \
            "重投必须保留原 type——否则消费侧判别联合认不出它（等于换了一种丢法）"

        # 持久化的那条必须仍可消费（不是只躺在队列里）
        await agent._dispatch_event(_task_results(survivors)[0])
        assert agent._turns.names == ["task-result-wf-1"]


# ══════════════════════════════════════════════════════════════════
# B3. 中断 + 重启：库里 delivered=False，下次启动补投得回来
# ══════════════════════════════════════════════════════════════════

class TestInterruptThenRestart:
    @pytest.mark.asyncio
    async def test_unconsumed_result_is_pending_and_redelivered_next_start(self):
        repo = _StatefulRepo()
        bus = EventBus()
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox=mailbox, repo=repo, bus=bus)
        push_mailbox_event(mailbox, WorkflowEventType.COMPLETED, {"result": "报告写好了"})

        await _drain_mailboxes(agent)
        assert repo.written[0]["delivered"] is False, \
            "入队 ≠ 投递成功：写库时必须是待补投"

        # 消费者在取走之前死了（这里用最粗暴的方式模拟：队列被整个排空且无人处理）
        killed = bus.drain()
        assert _task_results(killed), "前提：终态结果当时确实在队列里"
        assert repo.delivered == [], "没人消费过 → 库里不得有投递确认"

        # —— 重启：新进程、同一个库 ——
        bus2 = EventBus()
        agent2 = _stub_agent(repo=repo, bus=bus2)

        await _redeliver(agent2)

        reposted = _task_results(bus2.drain())
        assert [p.task_id for p in reposted] == ["wf-1"], \
            "未消费的终态记录必须在下次启动被重新投进总线（可恢复）"
        assert reposted[0].result == "报告写好了"
        assert repo.delivered == [], (
            "补投只是入队——投递确认必须等消费方真正接手（B-1：ack 不得早于所有权转移）")

        # 新进程里真正消费掉它
        await agent2._dispatch_event(reposted[0])
        assert agent2._turns.names == ["task-result-wf-1"]
        assert repo.delivered == ["wf-1"]

        # 消费过之后就不再是待补投（不会每次启动都重复告知）
        bus3 = EventBus()
        agent3 = _stub_agent(repo=repo, bus=bus3)
        await _redeliver(agent3)
        assert bus3.pending_count == 0


# ══════════════════════════════════════════════════════════════════
# B4. 不得重复告知：requeue 是搬移，恰好一次宣告
# ══════════════════════════════════════════════════════════════════

class TestNoDuplicateAnnouncement:
    @pytest.mark.asyncio
    async def test_exactly_one_announcement_across_interrupt_requeue(self):
        trace: list[str] = []
        repo = _StatefulRepo(trace=trace)
        bus = _TracedBus(trace)
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox=mailbox, repo=repo, bus=bus, trace=trace)
        push_mailbox_event(mailbox, WorkflowEventType.COMPLETED, {"result": "报告写好了"})

        assert await _drain_mailboxes(agent) == 1
        assert agent._background_tasks == {}, \
            "终态已投递 → mailbox 条目必须 pop，否则下轮排空会重复投递同一条"
        assert await _drain_mailboxes(agent) == 0, "同一 mailbox 事件不得被排空两次"
        assert bus.pending_count == 1, "队列里应当**恰好**一条终态结果"

        bus.post(InterruptData(source="ws_disconnect"),
                 priority=EventPriority.INTERRUPT)
        interrupt = await _next_event(agent)
        assert interrupt is not None
        await agent._dispatch_event(interrupt.data)

        assert bus.pending_count == 1, (
            "requeue 是**搬移**不是复制：中断排空再重投之后，队列里仍应恰好一条")

        consumed = await _next_event(agent)
        assert consumed is not None and isinstance(consumed.data, TaskResultData)
        await agent._dispatch_event(consumed.data)

        assert agent._turns.names == ["task-result-wf-1"], \
            "同一终态结果只允许 spawn 一次宣告回合（重复 = 重复告知用户）"
        assert repo.delivered == ["wf-1"], "投递确认恰好一次"
        assert bus.pending_count == 0


# ══════════════════════════════════════════════════════════════════
# B5. 禁止态不可达：ack 必须晚于"所有权转移"
# ══════════════════════════════════════════════════════════════════

class TestDurableAckFollowsOwnership:
    @pytest.mark.asyncio
    async def test_trace_is_persist_post_spawn_mark(self, tmp_path):
        """顺序证据：`落库(待投递) → 入队 → spawn(所有权转移) → mark_delivered`。"""
        from core.storage.task_result_wal import TaskResultWAL

        trace: list[str] = []
        repo = _StatefulRepo(trace=trace)
        bus = _TracedBus(trace)
        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_dispatch("wf-1", "写季度报告", _DISPATCHED_AT)
        wal.record_terminal("wf-1", "completed", {"result": "报告写好了"})
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox=mailbox, repo=repo, bus=bus, trace=trace, wal=wal)
        push_mailbox_event(mailbox, WorkflowEventType.COMPLETED, {"result": "报告写好了"})

        # ① 入队：先落库（delivered=False），再投递
        assert await _drain_mailboxes(agent) == 1
        assert repo.written[0]["delivered"] is False, \
            "写库时不得预先宣告已投递（入队只证明进了队列，不证明有人接手）"
        assert trace == ["persist", "post"], f"实得 {trace}"
        # WAL 副本在落库成功那一刻就清了 —— 此时这条结果**只**在队列里
        assert wal.read_raw() == []
        _assert_not_forbidden(agent, repo, wal, stage="入队后")
        assert repo.delivered == [], "还没消费，库里不得有投递确认"

        # ② 消费：spawn 成功之后才允许 mark
        consumed = await _next_event(agent)
        assert consumed is not None and isinstance(consumed.data, TaskResultData)
        await agent._dispatch_event(consumed.data)

        assert trace == ["persist", "post", "spawn", "mark"], (
            f"投递确认必须晚于宣告回合被接受，实得 {trace}")
        assert repo.delivered == ["wf-1"]
        _assert_not_forbidden(agent, repo, wal, stage="消费后")

    @pytest.mark.asyncio
    async def test_forbidden_triple_unreachable_across_interrupt(self, tmp_path):
        """整条管线（含一次中断搬移）逐步检查禁止三元组。"""
        from core.storage.task_result_wal import TaskResultWAL

        trace: list[str] = []
        repo = _StatefulRepo(trace=trace)
        bus = _TracedBus(trace)
        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_terminal("wf-1", "completed", {"result": "报告写好了"})
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox=mailbox, repo=repo, bus=bus, trace=trace, wal=wal)
        push_mailbox_event(mailbox, WorkflowEventType.COMPLETED, {"result": "报告写好了"})

        await _drain_mailboxes(agent)
        _assert_not_forbidden(agent, repo, wal, stage="排空入队后")

        bus.post(InterruptData(source="ws_disconnect"),
                 priority=EventPriority.INTERRUPT)
        interrupt = await _next_event(agent)
        assert interrupt is not None
        await agent._dispatch_event(interrupt.data)
        _assert_not_forbidden(agent, repo, wal, stage="中断排空后")
        assert repo.delivered == [], "中断处理本身绝不写投递确认"

        consumed = await _next_event(agent)
        assert consumed is not None and isinstance(consumed.data, TaskResultData)
        await agent._dispatch_event(consumed.data)
        _assert_not_forbidden(agent, repo, wal, stage="消费后")
        assert agent._turns.names == ["task-result-wf-1"]
        assert repo.delivered == ["wf-1"]

    @pytest.mark.asyncio
    async def test_spawn_failure_keeps_record_pending_and_consumer_alive(self):
        """spawn 抛异常 → **刻意不标记**已投递：记录留在库里待补投。

        B-1 的失败语义：异常按原样冒泡到 `_process_events` 的 per-event 边界
        （消费者不死），库里那条保持 `delivered=False`，下次启动补投。
        照单点菜是 **at-least-once**：宁可重复宣告一次，也不能静默丢掉用户等的
        那个结果——这里钉住的正是"不标记"这一半（另一半点在 B3 的补投用例）。
        """
        from core.agent.agent import Agent

        repo = _StatefulRepo()
        event = TaskResultData(
            task_id="wf-1", result="报告写好了", original_intent="写季度报告",
            dispatched_at=_DISPATCHED_AT)
        agent = _stub_agent(repo=repo)
        await repo.upsert_pending(task_id=event.task_id, result=event.result,
                                  original_intent=event.original_intent,
                                  dispatched_at=event.dispatched_at,
                                  status="completed", is_error=False, delivered=False)

        attempts: list[TaskResultData] = []

        def _boom(coro, **_kw):  # noqa: ANN001
            coro.close()      # 别留"未 await 的 coroutine"（pytest.ini 置为 error）
            attempts.append(event)
            raise RuntimeError("AsyncTaskScope 已关闭")

        agent._turns.spawn = _boom
        consumer = asyncio.create_task(Agent._process_events(agent))
        await asyncio.sleep(0)                    # 让消费者进入 await bus.next()
        agent._bus.post(event)

        for _ in range(200):                      # 等它处理（上限 1s，避免挂死）
            if attempts:
                break
            await asyncio.sleep(0.005)

        assert attempts == [event], "前提：消费者确实尝试过 spawn"
        assert not consumer.done(), \
            "spawn 异常必须被 per-event 边界吞掉——消费者不能因此退出（S3-03）"
        assert repo.delivered == [], "spawn 没成功 = 没人接手 = 不得写投递确认"
        assert repo.written[0]["delivered"] is False
        assert await repo.list_pending(), "记录必须仍是待补投（下次启动还能送达）"

        # 收尾：置 STOPPED 再投一条探针事件，让消费者从 await next() 里出来
        agent._state = RunState.STOPPED
        agent._bus.post(SystemTickData(reason="time_of_day", elapsed=0))
        await asyncio.wait_for(consumer, timeout=1.0)
