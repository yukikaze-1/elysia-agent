# 测试文件：core/agent/tests/test_task_result_durability.py
"""#6（决策 #6）：后台任务终态跨重启不丢 —— 停机落库 + 启动补投。

**原缺陷**（本文件钉住的每一条都对应它的一半）：

1. 停机窗口内到达的终态事件，`_stop_mailbox` 会 `bus.post` 出去——但事件消费者
   `_process_events` 是关停第一步就被取消的，**投进的是死队列**。用户永远等不到
   "任务完成了"，重启后连痕迹都没有。
2. 停机时仍未终态的任务（引擎被 cancel）只有一条 warning 日志，"用户等一个永远
   不会来的结果"这件事在重启后不可见。

**修复后的不变量**：

- 终态事件**先落库再投递**，且落库一律 `delivered=False`（不管 `_state` 是什么）——
  入队只证明"进了队列"，不证明"有人接手"；durable ack 的唯一授予点是消费侧的
  `_handle_task_result`（spawn 成功 = 所有权转移完成）。Issue B 之前这里按
  `_state == RUNNING` 标 `delivered=True`，那条判据把"消费者协程活着"当成了
  "这条结果已被认领"——见 `test_task_result_interrupt_delivery.py` 的两半缺陷；
- 落库失败**不得**阻断投递（Mongo 离线时"当下告诉用户"优先于"跨重启保障"）；
- 未见终态的任务落库为 `interrupted`（有终态可投，不再只是日志）；
- 启动步骤把 `delivered=False` 的记录重新投进总线（**只投递**，不标记；标记仍由
  消费侧完成——否则每次启动都会重复告知同一个结果）。
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
                 fail_list: bool = False, pending: list[dict] | None = None,
                 existing: list[dict] | None = None, fail_get: bool = False) -> None:
        self.written: list[dict] = []
        self.delivered: list[str] = []
        self.trace = trace
        self.fail_upsert = fail_upsert
        self.fail_list = fail_list
        self.fail_get = fail_get
        self._pending = pending or []
        #: 库里"已有"的记录（#6 硬杀补录的判重依据：**库是权威**）
        self._existing = {str(d["task_id"]): d for d in (existing or [])}
        self.get_calls: list[str] = []

    async def upsert_pending(self, **kwargs) -> None:  # noqa: ANN003
        if self.trace is not None:
            self.trace.append("persist")
        if self.fail_upsert:
            raise RuntimeError("mongo offline")
        self.written.append(kwargs)
        self._existing[str(kwargs["task_id"])] = dict(kwargs)

    async def mark_delivered(self, task_id) -> None:  # noqa: ANN001
        if self.trace is not None:
            self.trace.append("mark")
        self.delivered.append(str(task_id))

    async def list_pending(self, **_kw) -> list[dict]:  # noqa: ANN003
        if self.fail_list:
            raise RuntimeError("mongo offline")
        return list(self._pending)

    async def get(self, task_id) -> dict | None:  # noqa: ANN001
        self.get_calls.append(str(task_id))
        if self.fail_get:
            raise RuntimeError("mongo offline")
        return self._existing.get(str(task_id))


def _stub_agent(mailbox: WorkflowMailbox, *, repo=None, trace=None, intent="写季度报告",
                wal=None):
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
    agent._task_result_wal = wal                  # 未接线（None）→ WAL 全程 no-op
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
        assert repo.delivered == [], \
            "排空路径不授予 durable ack（Issue B / B-1）——关停路径更是如此"

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
# 2. 轮询路径：只入队、不授予投递确认（Issue B / B-1 后的语义）
# ══════════════════════════════════════════════════════════════════

class TestPollPathQueuesWithoutAck:
    """轮询路径（`_state == RUNNING`，消费者协程活着）与关停路径**同语义**。

    Issue B 之前这条路径按 `_state == RUNNING` 直接标 `delivered=True`——把"消费者
    协程活着"当成了"这条结果已被认领"。消费者可能在任何一处 await，而队列里的元素
    会被后到的 INTERRUPT 插队 drain 掉（`_handle_interrupt`），于是"库里已投递 +
    WAL 已清 + 无人消费"三处皆无。现在 `_state` 不再参与任何持久化判据。
    """

    @pytest.mark.asyncio
    async def test_running_state_leaves_record_pending_until_consumed(self):
        trace: list[str] = []
        repo = _StubRepo(trace=trace)
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox, repo=repo, trace=trace)
        assert agent._state == RunState.RUNNING
        push_mailbox_event(mailbox, WorkflowEventType.COMPLETED, {"result": "好了"})

        from core.agent.agent import Agent

        delivered = await Agent.drain_mailboxes_once(agent)

        assert delivered == 1
        assert trace == ["persist", "post"], (
            f"顺序必须是 落库 → 投递；投递确认属于消费侧，不在这里，实得 {trace}")
        assert repo.written[0]["delivered"] is False, \
            "入队 ≠ 认领：写库必须是待补投，等消费方真正接手后再标记"
        assert repo.delivered == [], (
            "`_state == RUNNING` 不得授予 durable ack——"
            "消费者活着只是假设，元素可能马上被 INTERRUPT 排空掉")


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
    async def test_pending_results_are_reposted_without_ack(self):
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
        assert repo.delivered == [], (
            "补投只入队——投递确认必须等消费方接手（Issue B / B-1）；"
            "在入队处标记就是原缺陷的同一根因（入队冒充认领）")
        # 消费侧授予 ack 的正向一半由
        # `test_task_result_interrupt_delivery.py::TestInterruptThenRestart` 钉死。

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


# ══════════════════════════════════════════════════════════════════
# 4. 硬杀窗口（#6 残余）：本地 WAL 同步落盘 + 启动补录
# ══════════════════════════════════════════════════════════════════

class TestHardKillWal:
    """SIGKILL/掉电窗口：终态已产生、还没被 mailbox 轮询落库时进程消失。

    这一类的**唯一**副本只可能是"终态产生那一刻已 fsync 到盘"的本地 WAL——
    优雅停机的同步排空跑不到（硬杀不给任何代码机会），仓储落库也来不及。
    """

    def test_added_startup_step_runs_before_redelivery(self):
        order = [name for name, _ in AgentLifecycle._STARTUP_STEPS]
        assert "task_result_wal_replay" in order
        assert order.index("task_result_wal_replay") < order.index(
            "task_result_redelivery"), \
            "WAL 补录必须先于补投——反了这批终态要等下一轮启动才送达"

    @pytest.mark.asyncio
    async def test_terminal_wal_record_is_upserted_as_undelivered(self, tmp_path):
        from core.agent.agent import Agent
        from core.storage.task_result_wal import TaskResultWAL

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_dispatch("wf-9", "整理会议纪要", "2026-09-15T01:00:00Z")
        wal.record_terminal("wf-9", "completed", {"result": "纪要已生成"})
        repo = _StubRepo()
        agent = _stub_agent(WorkflowMailbox("wf-x"), repo=repo, wal=wal)  # type: ignore[arg-type]

        await Agent._replay_task_result_wal(agent)

        assert len(repo.written) == 1
        row = repo.written[0]
        assert row["status"] == "completed"
        assert row["result"] == "纪要已生成"
        assert row["original_intent"] == "整理会议纪要", \
            "终态记录本身不含 intent——只有派发记录能留下它（硬杀后用户才知道是哪件事）"
        assert row["dispatched_at"] == "2026-09-15T01:00:00Z"
        assert row["delivered"] is False, "补录的记录必须待补投"
        assert wal.read_raw() == [], "补录成功即清理 WAL 副本"

    @pytest.mark.asyncio
    async def test_wording_matches_normal_drain_path(self, tmp_path):
        """硬杀补录与正常排空必须给出**同一条**文本（两条路径对称）。"""
        from core.agent._lifecycle_task_system import terminal_payload
        from core.agent.agent import Agent
        from core.storage.task_result_wal import TaskResultWAL

        for event, data in (
            ("failed", {"error": "工具崩了"}),
            ("cancelled", {"reason": "用户取消"}),
        ):
            wal = TaskResultWAL(str(tmp_path / f"{event}.jsonl"))
            wal.record_terminal("wf-9", event, data)
            repo = _StubRepo()
            agent = _stub_agent(WorkflowMailbox("wf-x"), repo=repo, wal=wal)  # type: ignore[arg-type]

            await Agent._replay_task_result_wal(agent)

            expected_text, expected_error = terminal_payload(event, data)
            assert repo.written[0]["result"] == expected_text
            assert repo.written[0]["is_error"] is expected_error is True
            assert repo.written[0]["status"] == event

    @pytest.mark.asyncio
    async def test_half_written_line_from_kill_is_skipped(self, tmp_path):
        """硬杀可能把最后一行写了一半——半行必须跳过，前面完整的记录照常补录。"""
        from core.agent.agent import Agent
        from core.storage.task_result_wal import TaskResultWAL

        path = tmp_path / "wal.jsonl"
        wal = TaskResultWAL(str(path))
        wal.record_terminal("wf-ok", "completed", {"result": "完整的一条"})
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"kind": "terminal", "task_id": "wf-cut", "eve')  # 半行

        repo = _StubRepo()
        agent = _stub_agent(WorkflowMailbox("wf-x"), repo=repo, wal=wal)  # type: ignore[arg-type]
        await Agent._replay_task_result_wal(agent)

        assert [w["task_id"] for w in repo.written] == ["wf-ok"]
        assert repo.written[0]["result"] == "完整的一条"

    @pytest.mark.asyncio
    async def test_dispatch_only_means_interrupted_with_intent(self, tmp_path):
        from core.agent._lifecycle_task_system import INTERRUPTED_BY_HARD_KILL_TEXT
        from core.agent.agent import Agent
        from core.storage.task_result_wal import TaskResultWAL

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_dispatch("wf-9", "跑一次全库扫描", "2026-09-15T02:00:00Z")
        repo = _StubRepo()
        agent = _stub_agent(WorkflowMailbox("wf-x"), repo=repo, wal=wal)  # type: ignore[arg-type]

        await Agent._replay_task_result_wal(agent)

        row = repo.written[0]
        assert row["status"] == "interrupted"
        assert row["result"] == INTERRUPTED_BY_HARD_KILL_TEXT
        assert row["is_error"] is True
        assert row["original_intent"] == "跑一次全库扫描"
        assert row["delivered"] is False

    @pytest.mark.asyncio
    async def test_row_already_in_repo_is_not_resurrected(self, tmp_path):
        """库是权威：库里已有（可能已 delivered=True）的记录，WAL 不得把它重置回待投。"""
        from core.agent.agent import Agent
        from core.storage.task_result_wal import TaskResultWAL

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_terminal("wf-9", "completed", {"result": "新文本（不该覆盖库）"})
        repo = _StubRepo(existing=[{
            "task_id": "wf-9", "result": "库里的旧文本", "delivered": True,
        }])
        agent = _stub_agent(WorkflowMailbox("wf-x"), repo=repo, wal=wal)  # type: ignore[arg-type]

        await Agent._replay_task_result_wal(agent)

        assert repo.written == [], "库里已有 → 不得再写（否则 delivered 被重置成 False）"
        assert repo.get_calls == ["wf-9"]
        assert wal.read_raw() == [], "副本冗余，清理掉"

    @pytest.mark.asyncio
    async def test_storage_offline_keeps_wal_for_next_start(self, tmp_path):
        """仓储不可用 → 一条都不补、WAL 原样保留（它是唯一副本，不能删）。"""
        from core.agent.agent import Agent
        from core.storage.task_result_wal import TaskResultWAL

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_terminal("wf-9", "completed", {"result": "结果"})
        repo = _StubRepo(fail_get=True)
        agent = _stub_agent(WorkflowMailbox("wf-x"), repo=repo, wal=wal)  # type: ignore[arg-type]

        await Agent._replay_task_result_wal(agent)

        assert repo.written == []
        assert [r["task_id"] for r in wal.read_raw()] == ["wf-9"], \
            "查库失败必须保留 WAL——删了就真丢了"

    @pytest.mark.asyncio
    async def test_upsert_failure_keeps_wal(self, tmp_path):
        from core.agent.agent import Agent
        from core.storage.task_result_wal import TaskResultWAL

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_terminal("wf-9", "completed", {"result": "结果"})
        repo = _StubRepo(fail_upsert=True)
        agent = _stub_agent(WorkflowMailbox("wf-x"), repo=repo, wal=wal)  # type: ignore[arg-type]

        await Agent._replay_task_result_wal(agent)

        assert [r["task_id"] for r in wal.read_raw()] == ["wf-9"]

    @pytest.mark.asyncio
    async def test_no_repo_keeps_wal(self, tmp_path):
        from core.agent.agent import Agent
        from core.storage.task_result_wal import TaskResultWAL

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_terminal("wf-9", "completed", {"result": "结果"})
        agent = _stub_agent(WorkflowMailbox("wf-x"), repo=None, wal=wal)  # type: ignore[arg-type]

        await Agent._replay_task_result_wal(agent)

        assert [r["task_id"] for r in wal.read_raw()] == ["wf-9"]

    @pytest.mark.asyncio
    async def test_no_wal_wired_is_noop(self):
        from core.agent.agent import Agent

        repo = _StubRepo()
        agent = _stub_agent(WorkflowMailbox("wf-x"), repo=repo, wal=None)  # type: ignore[arg-type]

        await Agent._replay_task_result_wal(agent)

        assert repo.written == [] and repo.get_calls == []


class TestTerminalWalWiring:
    """sink 的接线：终态产生 → 同步进 WAL；落库成功 → 清 WAL 副本。"""

    @pytest.mark.asyncio
    async def test_registration_writes_dispatch_record(self, tmp_path):
        """登记后台任务时同步落派发记录——终态记录不含 intent，只有这里能留下它。"""
        from core.agent.agent import Agent
        from core.storage.task_result_wal import TaskResultWAL

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox, wal=wal)
        agent._background_tasks = {}

        Agent._register_background_task(
            agent, "wf-7", mailbox, "整理本周会议纪要", "2026-09-15T03:00:00Z")

        raw = wal.read_raw()
        assert len(raw) == 1 and raw[0]["kind"] == "dispatch"
        assert raw[0]["task_id"] == "wf-7"
        assert raw[0]["intent"] == "整理本周会议纪要"
        assert raw[0]["dispatched_at"] == "2026-09-15T03:00:00Z"
        assert "wf-7" in agent._background_tasks

    @pytest.mark.asyncio
    async def test_terminal_push_writes_wal_synchronously(self, tmp_path):
        from core.agent.agent import Agent
        from core.storage.task_result_wal import TaskResultWAL

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox, wal=wal)

        # engine 的挂载动作：sink 是 Agent 的 bound method（同步）
        mailbox.terminal_sink = agent._record_terminal_wal
        push_mailbox_event(mailbox, WorkflowEventType.FAILED, {"error": "工具崩了"})

        raw = wal.read_raw()
        assert len(raw) == 1 and raw[0]["kind"] == "terminal"
        assert raw[0]["task_id"] == "wf-1"
        assert raw[0]["event"] == "failed"
        assert raw[0]["data"] == {"error": "工具崩了"}

    @pytest.mark.asyncio
    async def test_non_terminal_push_leaves_wal_untouched(self, tmp_path):
        from core.agent.agent import Agent
        from core.storage.task_result_wal import TaskResultWAL

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox, wal=wal)
        mailbox.terminal_sink = agent._record_terminal_wal

        push_mailbox_event(mailbox, WorkflowEventType.QUEUED, {"task_id": "wf-1"})
        push_mailbox_event(mailbox, WorkflowEventType.PROGRESS, {"step": "1/3"})

        assert wal.read_raw() == [], "非终态不落 WAL（不然每条 progress 一次 fsync）"

    @pytest.mark.asyncio
    async def test_duplicate_terminal_not_written_twice(self, tmp_path):
        from core.agent.agent import Agent
        from core.storage.task_result_wal import TaskResultWAL

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        mailbox = WorkflowMailbox("wf-1")  # type: ignore[arg-type]
        agent = _stub_agent(mailbox, wal=wal)
        mailbox.terminal_sink = agent._record_terminal_wal

        push_mailbox_event(mailbox, WorkflowEventType.COMPLETED, {"result": "先到"})
        push_mailbox_event(mailbox, WorkflowEventType.FAILED, {"error": "后到的重复终态"})

        assert [r["event"] for r in wal.read_raw()] == ["completed"], \
            "被终态唯一性守卫丢弃的重复终态不该进 WAL"

    @pytest.mark.asyncio
    async def test_persist_success_clears_wal_copy(self, tmp_path):
        from core.agent.agent import Agent
        from core.schema.event import TaskResultData
        from core.storage.task_result_wal import TaskResultWAL

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_dispatch("wf-1", "写报告", "2026-09-15T00:00:00Z")
        wal.record_terminal("wf-1", "completed", {"result": "结果"})
        repo = _StubRepo()
        agent = _stub_agent(WorkflowMailbox("wf-1"), repo=repo, wal=wal)  # type: ignore[arg-type]

        ok = await Agent._persist_task_result(
            agent,
            TaskResultData(task_id="wf-1", result="结果", original_intent="写报告",
                           dispatched_at="2026-09-15T00:00:00Z"),
            status="completed", delivered=True)

        assert ok is True
        assert wal.read_raw() == [], "落库成功 → WAL 副本冗余，必须清掉"

    @pytest.mark.asyncio
    async def test_persist_failure_keeps_wal_copy(self, tmp_path):
        from core.agent.agent import Agent
        from core.schema.event import TaskResultData
        from core.storage.task_result_wal import TaskResultWAL

        wal = TaskResultWAL(str(tmp_path / "wal.jsonl"))
        wal.record_terminal("wf-1", "completed", {"result": "结果"})
        repo = _StubRepo(fail_upsert=True)
        agent = _stub_agent(WorkflowMailbox("wf-1"), repo=repo, wal=wal)  # type: ignore[arg-type]

        ok = await Agent._persist_task_result(
            agent,
            TaskResultData(task_id="wf-1", result="结果", original_intent="写报告",
                           dispatched_at="2026-09-15T00:00:00Z"),
            status="completed", delivered=False)

        assert ok is False
        assert [r["task_id"] for r in wal.read_raw()] == ["wf-1"], \
            "落库失败 → WAL 是这条终态的唯一副本，绝不能清"
