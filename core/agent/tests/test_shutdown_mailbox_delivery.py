"""S2-A4 回归：关停窗口内后台任务的终态结果必须可达。

**原缺陷**：`Agent.stop()` 第一行就 `_state = STOPPED` 并
`_background_tasks.clear()`。轮询循环条件（`while self._state == RunState.RUNNING`）
随即为假 → 停机窗口（drain_cognitive + 4 个 flush + db_health，合计可达数十秒）
内完成 / 失败 / 取消的后台任务，其终态事件落在 mailbox 里**无人读**，而注册表条目
也已被清掉；重启路径会新建 WorkflowEngine、旧 mailbox 一并丢弃 —— 用户永远等不到
「任务完成」唤醒，日志里也什么都没有。

**修复后的不变量**（本文件逐条钉死）：
1. `_SHUTDOWN_STEPS` 中 `stop_task_system` 在 `stop_mailbox` **之前**——引擎先停完
   （所有任务终态 + 事件写进 mailbox），再做最后一次同步排空才能一次读全；
2. `stop()` 入口**不**清 `_background_tasks`，清理推迟到 `_stop_mailbox` 内部；
3. `_stop_mailbox` 先排空并投递、再取消轮询任务、最后清注册表；
4. 未见终态的任务记 warning（不留静默丢失）；
5. 轮询路径与关停路径**投递同一条 `TaskResultData`**（两条路径不得漂移）。
"""
import asyncio
import logging

import pytest

from core.agent.agent import Agent
from core.agent.lifecycle import AgentLifecycle
from core.agent._types import RunState, _BackgroundTaskEntry
from Module.workflow_contract import WorkflowEventType
from Module.workflow_engine.mailbox import WorkflowMailbox, push_mailbox_event


class _StubBus:
    """记录 post 内容的 EventBus 替身。"""

    def __init__(self) -> None:
        self.posted: list = []

    def post(self, data, **kwargs) -> None:  # noqa: ANN001
        self.posted.append(data)


class _StubLifecycleScope:
    """`AsyncTaskScope` 替身——stop() 收尾的兜底 drain。"""

    async def drain(self, *, timeout: float | None = None) -> int:
        return 0


class _StubSessionManager:
    """`SessionManager` 替身——stop() 收尾的 clear_work。"""

    def clear_work(self) -> None:
        return None


def _completed_mailbox(task_id: str = "wf-1", result: str = "报告写完了") -> WorkflowMailbox:
    """造一个含单个 completed 终态事件的**真实** WorkflowMailbox。

    刻意不用手搓事件对象：`push_mailbox_event` 是生产写入路径，终态单调性守卫、
    有界淘汰都在它里面，手搓形状会绕过这些（本项目已多次踩过"手搓 fixture 与
    真实形状不一致"）。
    """
    mb = WorkflowMailbox(task_id)  # type: ignore[arg-type]
    push_mailbox_event(mb, WorkflowEventType.COMPLETED, {"result": result})
    return mb


def _empty_mailbox(task_id: str = "wf-1") -> WorkflowMailbox:
    """空 mailbox——事件留到"停机窗口"里再推（见用例 2）。

    必须为空：`WorkflowMailbox` 有终态单调性守卫，同一个 task 推第二次终态会被
    静默丢弃（这正是它该有的行为），预先塞一条会让"窗口内推入"的那条被吃掉。
    """
    return WorkflowMailbox(task_id)  # type: ignore[arg-type]


def _make_stub_agent(mailbox: WorkflowMailbox) -> Agent:
    """最小 Agent 替身：真实 `stop()` + 真实 `_stop_mailbox`，其余步骤 no-op。

    其余关停步骤整体替换为空操作，是为了让本用例聚焦"注册表生命周期 + 排空投递"
    这一条链路；`_SHUTDOWN_STEPS` 本身仍是**真实类属性**（顺序由用例 1 单独钉死）。
    """
    agent = object.__new__(Agent)
    agent._state = RunState.RUNNING
    agent._pending_messages = []
    agent._background_tasks = {
        "wf-1": _BackgroundTaskEntry(
            intent="写季度报告",
            dispatched_at="2026-09-13T00:00:00Z",
            mailbox=mailbox,
        ),
    }
    agent._bus = _StubBus()
    agent._mailbox_task = None
    agent._lifecycle_tasks = _StubLifecycleScope()
    agent._session_manager = _StubSessionManager()

    async def _noop() -> None:
        return None

    for _name, method_name in AgentLifecycle._SHUTDOWN_STEPS:
        if method_name in ("_stop_mailbox", "_stop_task_system"):
            continue
        setattr(agent, method_name, _noop)
    return agent


# ═══════════════════════════════════════════════════════════════════
# 1. 关停步骤顺序（静态不变量）
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
def test_shutdown_stops_task_system_before_mailbox():
    """`stop_task_system` 必须排在 `stop_mailbox` 之前。

    反了的话 `_stop_mailbox` 会在引擎**仍在收尾**时做收尾排空——那时终态事件还没
    写进 mailbox，排空读到的是空队列，随后引擎才把事件写进去（且此时已无人再读）。
    """
    order = [name for name, _ in AgentLifecycle._SHUTDOWN_STEPS]
    assert "stop_task_system" in order and "stop_mailbox" in order
    assert order.index("stop_task_system") < order.index("stop_mailbox"), (
        "WorkflowEngine 必须先停完再做 mailbox 收尾排空，否则排空读到空队列"
    )


# ═══════════════════════════════════════════════════════════════════
# 2. 核心回归：停机窗口内完成的终态事件仍然可达
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
async def test_terminal_event_arriving_during_shutdown_window_is_delivered():
    """`_state` 已置 STOPPED 之后才完成的后台任务，其结果仍要投递到 EventBus。

    `_stop_task_system` 这一步模拟"引擎收尾期间任务完成"——它同时断言此刻注册表
    仍然有该条目（修复前 `stop()` 入口已 clear，这里会是 0），并在引擎停下之前把
    completed 事件写进 mailbox（与真实 WorkflowEngine.stop() 的时序一致）。
    """
    mailbox = _empty_mailbox()          # 终态事件在"停机窗口内"才推入
    agent = _make_stub_agent(mailbox)
    observed: dict[str, int] = {}

    async def _engine_stop() -> None:
        # 关键断言点：走到"引擎停机"这一步时，注册表条目必须还在
        observed["registry_size"] = len(agent._background_tasks)
        # 模拟引擎收尾期间任务完成（真实路径：WorkflowEngine.stop() 写终态事件）
        push_mailbox_event(mailbox, WorkflowEventType.COMPLETED, {"result": "迟到但送达"})
        observed["mailbox_events"] = mailbox.count()

    agent._stop_task_system = _engine_stop  # type: ignore[method-assign]

    await agent.stop()

    assert observed["registry_size"] == 1, (
        "stop() 入口不应清 _background_tasks——清了收尾排空就无条目可查（S2-A4 原缺陷）"
    )
    assert agent._bus.posted, "停机窗口内完成的终态事件必须投递到 EventBus（用户才等得到唤醒）"
    results = [p for p in agent._bus.posted if getattr(p, "task_id", None) == "wf-1"]
    assert len(results) == 1
    assert results[0].result == "迟到但送达"
    assert results[0].original_intent == "写季度报告"
    assert results[0].is_error is False
    # 已投递的条目必须清掉，否则 /restart 复用实例时残留（旧行为的问题）
    assert agent._background_tasks == {}
    assert agent._state == RunState.STOPPED


@pytest.mark.service
@pytest.mark.asyncio
async def test_shutdown_delivers_failed_and_cancelled_variants():
    """failed / cancelled 两种终态走关停路径同样要投递，且 `is_error=True`。"""
    for event_type, expect_fragment in (
        (WorkflowEventType.FAILED, "任务执行失败"),
        (WorkflowEventType.CANCELLED, "任务已取消"),
    ):
        mailbox = _empty_mailbox()
        agent = _make_stub_agent(mailbox)

        async def _engine_stop(_mb=mailbox, _t=event_type) -> None:
            if _t == WorkflowEventType.FAILED:
                push_mailbox_event(_mb, _t, {"error": "工具崩了"})
            else:
                push_mailbox_event(_mb, _t, {"reason": "用户取消"})

        agent._stop_task_system = _engine_stop  # type: ignore[method-assign]
        await agent.stop()

        results = [p for p in agent._bus.posted if getattr(p, "task_id", None) == "wf-1"]
        assert len(results) == 1, f"{event_type} 终态在关停路径上丢失"
        assert expect_fragment in results[0].result
        assert results[0].is_error is True


# ═══════════════════════════════════════════════════════════════════
# 3. 未见终态的任务：不留静默丢失
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
async def test_stranded_tasks_are_logged_not_silently_dropped(caplog):
    """引擎停完后仍无终态事件的任务 → warning 里能逐条看到（否则就是静默丢失）。"""
    mailbox = _empty_mailbox()
    agent = _make_stub_agent(mailbox)

    async def _engine_stop() -> None:
        return None

    agent._stop_task_system = _engine_stop  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="core.agent._lifecycle_task_system"):
        await agent.stop()

    assert not [p for p in agent._bus.posted if getattr(p, "task_id", None) == "wf-1"]
    assert agent._background_tasks == {}, "注册表仍须清空（防止 /restart 后残留）"
    messages = [r.getMessage() for r in caplog.records]
    assert any("未见终态" in m for m in messages), (
        f"未见终态的后台任务必须留 warning，实际日志: {messages}"
    )


# ═══════════════════════════════════════════════════════════════════
# 4. 两条路径对称：轮询投递 == 关停投递
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
async def test_poll_path_and_shutdown_path_deliver_identical_result():
    """同一 mailbox 内容经「轮询」与经「关停排空」投递出的 `TaskResultData` 必须一致。

    这是本项目反复出现的「两条路径不对称」缺陷族的直接防线：`_poll_mailboxes`
    与 `_stop_mailbox` 若各写一份投递逻辑，字段少填一个（如 `original_intent` /
    `dispatched_at` / `is_error`）不会有任何测试报警。
    """
    # 轮询路径
    poll_mailbox = _completed_mailbox()
    poll_agent = _make_stub_agent(poll_mailbox)
    poll_task = asyncio.create_task(Agent._poll_mailboxes(poll_agent))
    await asyncio.sleep(0.6)                     # 轮询周期 500ms，跑满一轮
    poll_agent._state = RunState.STOPPED
    await asyncio.wait_for(poll_task, timeout=3.0)

    # 关停路径
    stop_mailbox = _completed_mailbox()
    stop_agent = _make_stub_agent(stop_mailbox)
    await Agent._stop_mailbox(stop_agent)

    poll_results = [p for p in poll_agent._bus.posted if getattr(p, "task_id", None) == "wf-1"]
    stop_results = [p for p in stop_agent._bus.posted if getattr(p, "task_id", None) == "wf-1"]
    assert len(poll_results) == 1 and len(stop_results) == 1
    assert poll_results[0].model_dump() == stop_results[0].model_dump(), (
        "轮询路径与关停路径投递的任务结果不一致——两条路径漂移"
    )
