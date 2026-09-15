# 测试文件：core/agent/tests/test_task_result_durability.py
"""#6（决策 #6）：后台任务终态跨重启不丢 —— 停机落库 + 启动补投。

**原缺陷**（本文件钉住的每一条都对应它的一半）：

1. 停机窗口内到达的终态事件，`_stop_mailbox` 会 `bus.post` 出去——但事件消费者
   `_process_events` 是关停第一步就被取消的，**投进的是死队列**。用户永远等不到
   "任务完成了"，重启后连痕迹都没有。
2. 停机时仍未终态的任务（引擎被 cancel）只有一条 warning 日志，"用户等一个永远
   不会来的结果"这件事在重启后不可见。

**修复后的不变量**：

- 终态事件**先落库再投递**；`_state == RUNNING`（消费者活着）标 `delivered=True`，
  关停路径（消费者已死）保持 `False` → 下次启动补投；
- 落库失败**不得**阻断投递（Mongo 离线时"当下告诉用户"优先于"跨重启保障"）；
- 未见终态的任务落库为 `interrupted`（有终态可投，不再只是日志）；
- 启动步骤把 `delivered=False` 的记录重新投进总线并标记已投递。
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.agent._types import RunState, _BackgroundTaskEntry
from core.agent.lifecycle import AgentLifecycle
from Module.workflow_contract import WorkflowEventType
from Module.workflow_engine.mailbox import WorkflowMailbox, push_mailbox_event

pytestmark = pytest.mark.service


class _StubBus:
    """记录 post 内容与顺序的 EventBus 替身。"""

    def __init__(self, trace: list[str] | None = None) -> None:
        self.posted: list = []
        self.trace = trace

    def post(self, data, **kwargs) -> None:  # noqa: ANN001
        self.posted.append(data)
        if self.trace is not None:
            self.trace.append("post")


class _StubRepo:
    """终态结果仓储替身——记录调用顺序与写入内容，可注入失败。"""

    def __init__(self, *, trace: list[str] | None = None, fail_upsert: bool = False,
                 fail_list: bool = False, pending: list[dict] | None = None) -> None:
        self.written: list[dict] = []
        self.delivered: list[str] = []
        self.trace = trace
        self.fail_upsert = fail_upsert
        self.fail_list = fail_list
        self._pending = pending or []

    async def upsert_pending(self, **kwargs) -> None:  # noqa: ANN003
        if self.trace is not None:
            self.trace.append("persist")
        if self.fail_upsert:
            raise RuntimeError("mongo offline")
        self.written.append(kwargs)

    async def mark_delivered(self, task_id) -> None:  # noqa: ANN001
        if self.trace is not None:
            self.trace.append("mark")
        self.delivered.append(str(task_id))

    async def list_pending(self, **_kw) -> list[dict]:  # noqa: ANN003
        if self.fail_list:
            raise RuntimeError("mongo offline")
        return list(self._pending)


def _stub_agent(mailbox: WorkflowMailbox, *, repo=None, trace=None, intent="写季度报告"):
    """最小 Agent 替身：真实 `_stop_mailbox` / `_poll_mailboxes`，其余关停步骤 no-op。"""
    from core.agent.agent import Agent

    agent = object.__new__(Agent)
    agent._state = RunState.RUNNING
    agent._pending_messages = []
    agent._background_tasks = {
        "wf-1": _BackgroundTaskEntry(
            intent=intent,
            dispatched_at="2026-09-15T00:00:00Z",
            mailbox=mailbox,
        ),
    }
    agent._bus = _StubBus(trace)
    agent._mailbox_task = None
    agent._lifecycle_tasks = MagicMock()
    agent._session_manager = MagicMock()
    agent._background_task_result_repo = repo     # 未接线的替身场景显式置 None
    return agent


async def _stop_agent(agent) -> None:
    """只跑 `_stop_mailbox` 一步（其余步骤与本项无关）。"""
    await type(agent)._stop_mailbox(agent)


# ══════════════════════════════════════════════════════════════════
# 1. 关停路径：落库（delivered=False）且先落库再投递
# ══════════════════════════════════════════════════════════════════

class TestShutdownPersistsBeforePosting:
    @pytest.mark.asyncio
    async def test_shutdown_window_terminal_lands_as_undelivered(self):
        trace: list[str] = []
        repo = _StubRepo(trace=trace)
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox, repo=repo, trace=trace)
        agent._state = RunState.STOPPED          # 关停途中
        push_mailbox_event(mailbox, WorkflowEventType.COMPLETED, {"result": "迟到但送达"})

        await _stop_agent(agent)

        assert [w["status"] for w in repo.written] == ["completed"]
        assert repo.written[0]["delivered"] is False, \
            "关停路径的总线是死队列（消费者已被取消）——必须留成待补投"
        assert repo.written[0]["result"] == "迟到但送达"
        assert repo.written[0]["original_intent"] == "写季度报告"
        assert repo.written[0]["dispatched_at"] == "2026-09-15T00:00:00Z"
        assert trace == ["persist", "post"], \
            f"落库必须先于投递（否则崩溃窗口内记录缺失）——实得 {trace}"
        assert repo.delivered == [], "消费者已死，不得标记已投递"

    @pytest.mark.asyncio
    async def test_failed_and_cancelled_map_to_status(self):
        for event_type, status in ((WorkflowEventType.FAILED, "failed"),
                                   (WorkflowEventType.CANCELLED, "cancelled")):
            repo = _StubRepo()
            mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
            agent = _stub_agent(mailbox, repo=repo)
            agent._state = RunState.STOPPED
            data = {"error": "工具崩了"} if event_type == WorkflowEventType.FAILED else {
                "reason": "用户取消"}
            push_mailbox_event(mailbox, event_type, data)

            await _stop_agent(agent)

            assert [w["status"] for w in repo.written] == [status]
            assert repo.written[0]["is_error"] is True

    @pytest.mark.asyncio
    async def test_stranded_task_persisted_as_interrupted(self):
        """未见终态的任务 → 落库 interrupted（不能只有一行日志）。"""
        repo = _StubRepo()
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox, repo=repo)

        await _stop_agent(agent)

        assert len(repo.written) == 1
        assert repo.written[0]["status"] == "interrupted"
        assert repo.written[0]["delivered"] is False
        assert repo.written[0]["is_error"] is True
        assert "重启" in repo.written[0]["result"]
        assert agent._background_tasks == {}
        assert not agent._bus.posted, "关停路径不往死总线投递合成终态（只落库待补投）"

    @pytest.mark.asyncio
    async def test_persist_failure_does_not_block_delivery(self):
        """Mongo 离线 → 落库失败只记 warning，投递照旧（当下告诉用户优先）。"""
        repo = _StubRepo(fail_upsert=True)
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox, repo=repo)
        push_mailbox_event(mailbox, WorkflowEventType.COMPLETED, {"result": "结果"})

        # 不抛：落库失败被 _persist_task_result 吞成 warning（并保留投递）
        await _stop_agent(agent)

        assert [p.task_id for p in agent._bus.posted] == ["wf-1"]
        assert not repo.written

    @pytest.mark.asyncio
    async def test_no_repo_configured_is_tolerated(self):
        """替身/未接线场景（repo=None）→ 只投递，不抛（既有用例的形状）。"""
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox, repo=None)
        push_mailbox_event(mailbox, WorkflowEventType.COMPLETED, {"result": "r"})

        await _stop_agent(agent)

        assert len(agent._bus.posted) == 1


# ══════════════════════════════════════════════════════════════════
# 2. 轮询路径：消费者活着 → 落库即标已投递（不留重复补投）
# ══════════════════════════════════════════════════════════════════

class TestPollPathMarksDelivered:
    @pytest.mark.asyncio
    async def test_running_state_marks_delivered(self):
        trace: list[str] = []
        repo = _StubRepo(trace=trace)
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox, repo=repo, trace=trace)
        assert agent._state == RunState.RUNNING
        push_mailbox_event(mailbox, WorkflowEventType.COMPLETED, {"result": "好了"})

        from core.agent.agent import Agent

        delivered = await Agent.drain_mailboxes_once(agent)

        assert delivered == 1
        assert trace == ["persist", "post", "mark"], \
            f"顺序必须是 落库 → 投递 → 标已投递，实得 {trace}"
        assert repo.written[0]["delivered"] is True
        assert repo.delivered == ["wf-1"], "消费者活着 → 不得留在待补投集合里"


# ══════════════════════════════════════════════════════════════════
# 3. 启动补投
# ══════════════════════════════════════════════════════════════════

class TestStartupRedelivery:
    def test_step_registered_after_event_consumer(self):
        order = [name for name, _ in AgentLifecycle._STARTUP_STEPS]
        assert "task_result_redelivery" in order
        assert order.index("idle_and_events") < order.index("task_result_redelivery"), \
            "补投必须在事件消费者启动之后（否则又投进没人读的队列）"

    @pytest.mark.asyncio
    async def test_pending_results_are_reposted_and_marked(self):
        from core.agent.agent import Agent

        repo = _StubRepo(pending=[{
            "task_id": "wf-1",
            "result": "上次进程里跑完的任务结果",
            "original_intent": "写报告",
            "dispatched_at": "2026-09-14T00:00:00Z",
            "is_error": False,
            "status": "completed",
        }])
        agent = _stub_agent(WorkflowMailbox("wf-x"), repo=repo)  # type: ignore[arg-type]

        await Agent._redeliver_pending_task_results(agent)

        assert len(agent._bus.posted) == 1
        posted = agent._bus.posted[0]
        assert posted.task_id == "wf-1"
        assert posted.result == "上次进程里跑完的任务结果"
        assert posted.original_intent == "写报告"
        assert posted.dispatched_at == "2026-09-14T00:00:00Z"
        assert posted.is_error is False
        assert repo.delivered == ["wf-1"], "补投后必须标记，否则每次启动都重复告知"

    @pytest.mark.asyncio
    async def test_storage_offline_keeps_records_for_next_start(self, caplog):
        from core.agent.agent import Agent

        repo = _StubRepo(fail_list=True)
        agent = _stub_agent(WorkflowMailbox("wf-x"), repo=repo)  # type: ignore[arg-type]

        with caplog.at_level(logging.WARNING):
            await Agent._redeliver_pending_task_results(agent)

        assert not agent._bus.posted
        assert [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    async def test_no_pending_is_noop(self):
        from core.agent.agent import Agent

        repo = _StubRepo(pending=[])
        agent = _stub_agent(WorkflowMailbox("wf-x"), repo=repo)  # type: ignore[arg-type]

        await Agent._redeliver_pending_task_results(agent)

        assert not agent._bus.posted
        assert repo.delivered == []
