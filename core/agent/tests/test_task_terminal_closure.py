"""Batch 1.7 跨层回归：planning 失败的终态必须走既有 durable 链路，归因不被改写。

engine 侧的缺陷（`Module/workflow_engine/tests/unit/test_terminal_state_closure.py`）
在 core 侧表现为**两条错误归因**，本文件各钉一条：

1. **停机**：planning 失败的任务在 mailbox 里只剩 QUEUED → `_stop_mailbox` 把
   `_background_tasks` 里那条当成 "stranded"，落库为 `interrupted` +
   `"任务因服务重启被中断，未产生结果"`——而真实原因是**规划失败**，
   进程根本没被打断，"未产生结果"也与之矛盾（原因明明已经产生了）。
2. **硬杀**：WAL 里只有派发记录、没有终态记录 → `_replay_task_result_wal`
   补 `INTERRUPTED_BY_HARD_KILL_TEXT`（"任务因服务进程异常终止被中断，未产生结果"）
   ——同样是错误归因。

修复后两条路径都读到**真实终态**（`failed` + 规划失败原因），措辞经同一个
`terminal_payload` 实现点，**不被改写**（不变量 T3/T4）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from Module.workflow_contract import (
    AgentInfra,
    ConstraintConfig,
    DelegationContext,
    SessionId,
)
from Module.workflow_engine.mailbox import WorkflowMailbox
from Module.workflow_engine.workflow_engine import WorkflowEngine
from core.agent._lifecycle_task_system import INTERRUPTED_BY_HARD_KILL_TEXT
from core.agent._types import RunState, _BackgroundTaskEntry
from core.storage.task_result_wal import TaskResultWAL

pytestmark = pytest.mark.service


# ── 替身 ────────────────────────────────────────────────────────────────────


class _RaisingLLM:
    """最小 fake LLM：planner 的 create 恒定抛异常（模拟 LLM/网络故障）。"""

    def __init__(self) -> None:
        self.chat = self
        self.completions = self
        self.calls = 0

    async def create(self, *, messages: list[dict], **kwargs: Any) -> Any:
        self.calls += 1
        raise RuntimeError("planner LLM 不可用（模拟）")


class _StubBus:
    """记录 post 内容的事件总线替身。"""

    def __init__(self) -> None:
        self.posted: list = []

    def post(self, data, **kwargs) -> None:  # noqa: ANN001
        self.posted.append(data)


class _StubRepo:
    """终态结果仓储替身——记录写入内容（断言 status / result 的观测点）。"""

    def __init__(self, *, pending: list[dict] | None = None) -> None:
        self.written: list[dict] = []
        self.delivered: list[str] = []
        self._existing: dict[str, dict] = {}
        self._pending = pending or []

    async def upsert_pending(self, **kwargs) -> None:  # noqa: ANN003
        self.written.append(dict(kwargs))
        self._existing[str(kwargs["task_id"])] = dict(kwargs)

    async def mark_delivered(self, task_id) -> None:  # noqa: ANN001
        self.delivered.append(str(task_id))

    async def list_pending(self, **_kw) -> list[dict]:  # noqa: ANN003
        return list(self._pending)

    async def get(self, task_id) -> dict | None:  # noqa: ANN001
        return self._existing.get(str(task_id))


# ── 基建 ────────────────────────────────────────────────────────────────────


def _failing_engine(wal: TaskResultWAL) -> WorkflowEngine:
    """planner 恒失败的 WorkflowEngine，终态 sink 接到**真实 WAL**。"""
    infra = AgentInfra(
        llm=_RaisingLLM(), milvus=None, http=None,
        tool_registry=object(), constraints=ConstraintConfig(),
        terminal_result_sink=lambda tid, event, data: wal.record_terminal(tid, event, data),
    )
    from unittest.mock import MagicMock
    return WorkflowEngine(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=1)


async def _submit_and_settle(engine: WorkflowEngine, *, intent: str = "写季度报告") -> WorkflowMailbox:
    """提交任务并等到执行包装收尾（注册表摘除 ⇒ 终态事件已推）。"""
    ctx = DelegationContext(intent=intent, tools=[], session_id=SessionId("s1"))
    result = await engine.submit(ctx)
    assert isinstance(result, WorkflowMailbox), f"submit 被拒: {result}"
    tid = result.task_id
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if tid not in engine._tasks:
            await asyncio.sleep(0)
            return result
        await asyncio.sleep(0.005)
    raise AssertionError(f"任务 {tid} 未在超时内结束")


def _core_stub(mailbox: WorkflowMailbox, *, repo: _StubRepo, wal: TaskResultWAL,
               intent: str = "写季度报告"):
    """最小 Agent 替身：真实 `_stop_mailbox` / `_replay_task_result_wal`。"""
    from core.agent.agent import Agent

    agent = object.__new__(Agent)
    agent._state = RunState.RUNNING
    agent._background_tasks = {
        mailbox.task_id: _BackgroundTaskEntry(
            intent=intent,
            dispatched_at="2026-09-17T00:00:00Z",
            mailbox=mailbox,
        ),
    }
    agent._bus = _StubBus()
    agent._mailbox_task = None
    agent._background_task_result_repo = repo
    agent._task_result_wal = wal
    return agent


# ══════════════════════════════════════════════════════════════════════════
# 1. 停机路径：planning 失败不得被落库成 interrupted
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_planning_failure_persists_as_failed_not_interrupted(tmp_path):
    """planning 失败 → 停机排空落库为 **failed + 规划失败原因**，不是 interrupted。"""
    wal = TaskResultWAL(path=str(tmp_path / "wal.jsonl"))
    engine = _failing_engine(wal)
    await engine.start()
    try:
        mailbox = await _submit_and_settle(engine)
    finally:
        await engine.stop()

    repo = _StubRepo()
    agent = _core_stub(mailbox, repo=repo, wal=wal)
    await type(agent)._stop_mailbox(agent)

    assert len(repo.written) == 1, (
        f"应恰好落库一条终态结果，实际: {[w.get('status') for w in repo.written]}"
    )
    record = repo.written[0]
    assert record["status"] == "failed", (
        f"planning 失败必须落库为 failed，而不是被当成'被重启打断'；实际: {record['status']!r}"
    )
    assert "规划" in record["result"], (
        f"归因必须保留真实失败阶段（规划），实际: {record['result']!r}"
    )
    assert record["is_error"] is True
    assert not any(w.get("status") == "interrupted" for w in repo.written), (
        "不得再出现 interrupted 记录——那正是修复前的错误归因"
    )
    # 终态已投递 → 注册表条目被摘除（不再作为 stranded 留存）
    assert mailbox.task_id not in agent._background_tasks


# ══════════════════════════════════════════════════════════════════════════
# 2. 硬杀路径：WAL 补投必须保留 planning 归因
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_hard_kill_replay_preserves_planning_attribution(tmp_path):
    """硬杀后 WAL 补投 → **failed + 规划失败原因**，不是"进程异常终止"的泛化文案。"""
    wal = TaskResultWAL(path=str(tmp_path / "wal.jsonl"))
    engine = _failing_engine(wal)
    await engine.start()
    try:
        mailbox = await _submit_and_settle(engine)
    finally:
        await engine.stop()

    # 派发记录（生产由 Agent._register_background_task 同步落盘）
    wal.record_dispatch(str(mailbox.task_id), "写季度报告", "2026-09-17T00:00:00Z")

    raw = wal.read_raw()
    terminal_lines = [r for r in raw if r.get("kind") == "terminal"]
    assert terminal_lines, f"硬杀窗口依赖的终态 WAL 记录缺失，实际: {raw}"

    repo = _StubRepo()
    agent = _core_stub(mailbox, repo=repo, wal=wal)
    await type(agent)._replay_task_result_wal(agent)

    assert len(repo.written) == 1, f"应补录一条，实际: {repo.written}"
    record = repo.written[0]
    assert record["status"] == "failed", (
        f"补投必须保留真实终态 failed，实际: {record['status']!r}"
    )
    assert "规划" in record["result"], (
        f"硬杀补投不得把规划失败改写成通用文案，实际: {record['result']!r}"
    )
    assert record["result"] != INTERRUPTED_BY_HARD_KILL_TEXT, (
        "只有在'有派发无终态'时才允许用 interrupted 文案"
    )
    # 补录成功 → WAL 副本清理（库为权威）
    assert [r for r in wal.read_raw() if r.get("kind") == "terminal"] == []


# ══════════════════════════════════════════════════════════════════════════
# 3. 重启路径：已落库未消费的终态原样补投，归因不被改写
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_restart_redelivers_persisted_planning_failure_unchanged(tmp_path):
    """T6：终态已落库但尚未被消费 → 重启后仍正确投递，**failure reason 逐字不变**。

    "不改写"是可断言的：持久化文本与补投文本必须**逐字一致**——补投路径只搬
    `result`，任何重新措辞都会让它与库里的记录分叉。
    """
    wal = TaskResultWAL(path=str(tmp_path / "wal.jsonl"))
    engine = _failing_engine(wal)
    await engine.start()
    try:
        mailbox = await _submit_and_settle(engine)
    finally:
        await engine.stop()

    # ── 进程 A：停机排空 → 落库（delivered=False），用户尚未消费 ──
    repo = _StubRepo()
    agent_a = _core_stub(mailbox, repo=repo, wal=wal)
    await type(agent_a)._stop_mailbox(agent_a)
    assert len(repo.written) == 1
    persisted = repo.written[0]
    assert persisted["status"] == "failed" and "规划" in persisted["result"]

    # ── 进程 B：新 Agent、同一仓储 → 启动补投 ──
    repo._pending = [dict(persisted)]
    agent_b = _core_stub(mailbox, repo=repo, wal=wal)
    await type(agent_b)._redeliver_pending_task_results(agent_b)

    assert len(agent_b._bus.posted) == 1, "重启后必须补投那条未消费的终态"
    payload = agent_b._bus.posted[0]
    assert payload.result == persisted["result"], (
        "补投不得改写失败原因——持久化文本与投递文本必须逐字一致"
    )
    assert "规划" in payload.result
    assert str(payload.task_id) == str(mailbox.task_id)
    assert payload.is_error is True
    # Issue B：补投**只投递、不授予 durable ack**（确认权在消费侧）
    assert repo.delivered == []


# ══════════════════════════════════════════════════════════════════════════
# 4. 停机：轮询在落库中途被掐断，不得覆写真实终态
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cancelled_poll_does_not_let_shutdown_overwrite_real_terminal(tmp_path):
    """轮询在落库中途被停机掐断 → stranded 分支**不得**把真实终态覆写成 interrupted。

    Challenger C4：修复前 `_background_tasks.pop()` 排在
    `await self._persist_task_result(...)` **之后**，而 `_stop_mailbox` 的
    `cancel_and_drain` 恰好能在这个 await 上掐断轮询 ⇒ 条目仍在 ⇒ stranded 分支
    落库 `interrupted`（"任务因服务重启被中断，未产生结果"）**覆写**真实终态；
    接着启动 replay 把 `interrupted` 当权威、`wal.forget()` 删掉真正的终态——
    用户被告知"被中断"，而真实终态已被销毁（T2 + T4 双违反）。
    修复后条目在**取走事件之后、任何 await 之前**就摘除，窗口结构上不存在。
    """
    wal = TaskResultWAL(path=str(tmp_path / "wal.jsonl"))
    engine = _failing_engine(wal)
    await engine.start()
    try:
        mailbox = await _submit_and_settle(engine)
    finally:
        await engine.stop()

    class _HangingRepo:
        """落库时挂住（模拟 Mongo 黑洞）——制造"已接手、未落库完"的窗口。"""

        def __init__(self) -> None:
            self.written: list[tuple] = []
            self.hang = asyncio.Event()

        async def upsert_pending(self, **kwargs) -> None:  # noqa: ANN003
            self.written.append((kwargs.get("status"), str(kwargs.get("result"))))
            await self.hang.wait()          # 永远不返回：轮询会卡在这个 await 上

        async def mark_delivered(self, task_id) -> None:  # noqa: ANN001
            return None

    repo = _HangingRepo()
    agent = _core_stub(mailbox, repo=repo, wal=wal)

    poll = asyncio.create_task(type(agent).drain_mailboxes_once(agent))
    await asyncio.sleep(0.1)                # 让它取走终态事件并卡在落库 await 上
    poll.cancel()
    await asyncio.gather(poll, return_exceptions=True)

    assert mailbox.task_id not in agent._background_tasks, (
        "终态事件一旦被取走，注册表条目就必须已摘除（否则停机 stranded 分支会"
        "把真实终态覆写成 interrupted）"
    )

    repo.hang.set()
    await type(agent)._stop_mailbox(agent)

    assert not any(w[0] == "interrupted" for w in repo.written), (
        f"不得覆写真实终态；实际落库序列: {[w[0] for w in repo.written]}"
    )
    # 真实终态仍在 WAL 里 → 下次启动 replay 能补回（库为权威之前，WAL 是唯一副本）
    wal_terminals = [r for r in wal.read_raw() if r.get("kind") == "terminal"]
    assert wal_terminals, "落库未完成时 WAL 必须保留终态副本，否则跨重启真丢"
