# 测试文件：core/agent/tests/test_task_outcome_user_semantics.py
"""Batch 1.8：后台任务终态的**用户可见语义**必须等于真实 logical terminal outcome。

**原缺陷**（本文件逐条钉死）：终态真相在最后一段链路上被压扁成一句固定的
"任务已完成"。

    WorkflowEngine 终态（completed / failed / cancelled）
      → mailbox 事件          ✅ 三种终态齐备（Batch 1.7 已保证终态必然到达）
      → terminal_payload()    ✅ failed/cancelled 各有专属措辞 + is_error=True
      → TaskResultData        ⚠️ 只有 is_error 布尔：**failed 与 cancelled 无法区分**
      → EventBus              ✅ 原样搬运
      → _handle_task_result   ✅ 原样 model_dump() 成 ctx
      → _build_wake_up_message ❌ **完全不读 is_error**，恒定输出
                                 "已执行完毕 … 请自然地告知用户任务已完成"

于是出现标题所述的三重不一致：

    内部 truth = failed · 持久化 truth = failed · 恢复 truth = failed
    而用户看到的消息 = "任务已完成"

**为什么用户真的会看到**：这条消息不是给人看的固定文案，而是**模型上下文**——它
是本轮 `process()` 的 user message（`_prepare_think_input` 的返回值），LLM 据此
生成对用户的自然语言回复。上下文里写着"已完成"，LLM 就会照着说。所以"事实被写反"
与"用户被误导"之间只隔一次生成。

**修复后的不变量**（Batch 1.8 §5）：

- U1 用户可见语义 == logical terminal outcome（completed / failed / cancelled）；
- U2 内部细节可以压缩（planning_failed / execution_failed / interrupted 都可压成
  "失败"），但**不得反转**成成功；
- U3 live 投递与 restart 补投的语义一致；
- U4 at-least-once 重投不得改变语义。

**测试策略**：本文件刻意不只测 helper。绝大多数用例走**真实链路**
（mailbox → `drain_mailboxes_once` → EventBus → `_handle_task_result` → 认知输入），
只有最后一步的措辞构建用真实静态方法，避免"helper 对了但中间某层把 status 丢掉"。
措辞断言走**语义分类**（是否宣称成功 / 失败 / 取消）而不是逐字比对，这样文案演进
不会误伤测试，而"failed 被说成已完成"这条不变量永远拦得住。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from core.agent._types import RunState, _BackgroundTaskEntry
from core.agent.tests.test_task_result_durability import _StubRepo
from core.agent.tests.test_task_result_interrupt_delivery import _StatefulRepo, _StubScope
from core.cognitive import CognitiveLoop
from core.monitoring.event_bus import EventBus
from core.schema import event as event_module
from core.schema.event import TaskResultData
from Module.workflow_contract import WorkflowEventType
from Module.workflow_engine.mailbox import WorkflowMailbox, push_mailbox_event

pytestmark = pytest.mark.service

_DISPATCHED_AT = "2026-09-15T00:00:00Z"

#: 三种用户级语义（与生产映射的值域一致；用裸 str 比对——StrEnum 成员与自身
#: 字符串值相等，故生产侧无论返回枚举成员还是裸串，断言都成立）。
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

#: 三种终态对应的引擎事件（顺序扰动等用例共用同一份定义，避免逐处手写漂移）。
_TERMINAL_EVENTS = {
    COMPLETED: (WorkflowEventType.COMPLETED, {"result": "做完了"}),
    FAILED: (WorkflowEventType.FAILED, {"error": "崩了"}),
    CANCELLED: (WorkflowEventType.CANCELLED, {"reason": "用户取消"}),
}


# ══════════════════════════════════════════════════════════════════
# 语义判定：从**文本**反推它宣称了什么（与具体文案解耦）
# ══════════════════════════════════════════════════════════════════
#
# 用复合词而不是单字（"完成" / "成功"）：单字会被"未能完成"这类否定式命中，
# 反而把失败误判成成功。这里只认**明确的成功宣称**。

_SUCCESS_CLAIMS = ("已完成", "已经完成", "任务完成", "成功完成", "顺利完成")
_FAILURE_CLAIMS = ("未能完成", "失败")
_CANCEL_CLAIMS = ("已取消", "已经取消", "取消")


def _semantic(message: str) -> str:
    """把一条唤醒消息归类为用户级语义；无法判定 → "ambiguous"。"""
    if any(claim in message for claim in _SUCCESS_CLAIMS):
        return COMPLETED
    if any(claim in message for claim in _CANCEL_CLAIMS):
        return CANCELLED
    if any(claim in message for claim in _FAILURE_CLAIMS):
        return FAILED
    return "ambiguous"


def _claims_success(message: str) -> bool:
    return any(claim in message for claim in _SUCCESS_CLAIMS)


def _user_outcome(ctx: dict) -> str:
    """走**生产映射**把 ctx 压成用户级语义（Batch 1.8 的唯一定义点）。

    这里用 `getattr` 取符号而不是顶层 import：本文件是 **pre-fix 红测试**，修复前
    该符号不存在。顶层 import 会让整个文件在收集期就 ImportError，把"每条不变量
    各自的失败原因"塌缩成一条导入错误——那样就没有可读的红了。
    """
    mapper = getattr(event_module, "task_user_outcome", None)
    assert mapper is not None, (
        "缺少「内部终态 → 用户级语义」的映射函数"
        "（core.schema.event.task_user_outcome）——措辞点只能自己猜字符串，"
        "正是本批缺陷的成因（Batch 1.8 §8/§10）"
    )
    return str(mapper(str(ctx.get("status") or ""), bool(ctx.get("is_error", False))))


# ══════════════════════════════════════════════════════════════════
# 替身：真实 Agent 方法 + 可记录 ctx 的认知循环替身
# ══════════════════════════════════════════════════════════════════

class _RecordingCognitive:
    """记录 `process(task_result_context=...)` 的认知循环替身。

    `process` 刻意是**同步**函数并返回哨兵对象：真实 `process` 是 async，而
    `_handle_task_result` 把返回值交给 `spawn`；若这里产生协程，会撞上 pytest.ini
    的 `error:coroutine .*was never awaited`（无人 await 的协程是错误，不是噪音）。
    本文件只需要"消费方实际收到了什么 ctx"。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.current_task = None

    def cancel_current(self, **_kw) -> None:  # noqa: ANN003
        return None

    def process(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        return MagicMock(name="announce-turn")

    @property
    def last_ctx(self) -> dict:
        assert self.calls, "认知循环从未被唤醒——终态结果没有走到消费方"
        return self.calls[-1]["task_result_context"]


def _stub_agent(*, repo=None, bus=None, tasks: dict | None = None):
    """最小 Agent 替身：**真实** `drain_mailboxes_once` / `_handle_task_result`。

    `tasks` 形如 `{task_id: (intent, WorkflowMailbox)}`；一个替身可同时挂多个任务，
    用于顺序扰动用例（验证 outcome 不会在任务之间串台）。
    """
    from core.agent.agent import Agent

    agent = object.__new__(Agent)
    agent._state = RunState.RUNNING
    agent._pending_messages = []
    agent._req_counter = 0
    agent._background_tasks = {
        task_id: _BackgroundTaskEntry(
            intent=intent, dispatched_at=_DISPATCHED_AT, mailbox=mailbox)
        for task_id, (intent, mailbox) in (tasks or {}).items()
    }
    agent._bus = bus if bus is not None else EventBus()
    agent._cognitive = _RecordingCognitive()
    agent._turns = _StubScope()
    agent._mailbox_task = None
    agent._lifecycle_tasks = MagicMock()
    agent._session_manager = MagicMock()
    agent._background_task_result_repo = repo
    agent._task_result_wal = None
    return agent


def _restarted_with_same_record(repo: _StubRepo) -> _StubRepo:
    """模拟进程重启：把进程 A 落库的那一行当成**待补投**记录交给新进程。

    不能直接把进程 A 的 repo 交给"重启后"的替身：live 消费成功会授予 durable ack
    （`mark_delivered`），那条记录照设计就不再补投了（否则每次启动都重复告知）。
    本助手重建的正是"进程在消费前挂掉"时 Mongo 里会读到的那一行——字段与落库时
    完全一致，只有 `delivered` 回到 False。
    """
    return _StubRepo(pending=[{**row, "delivered": False} for row in repo.written])


async def _next_payload(agent, timeout: float = 1.0) -> TaskResultData:
    """取总线上的下一条 `TaskResultData`（拿不到就显式失败，不挂死）。"""
    while True:
        try:
            event = await asyncio.wait_for(agent._bus.next(), timeout=timeout)
        except asyncio.TimeoutError:  # pragma: no cover - 失败路径
            raise AssertionError("总线上没有终态结果——终态链路断了") from None
        if isinstance(event.data, TaskResultData):
            return event.data


def _mailbox(task_id: str, event_type: WorkflowEventType, data: dict) -> WorkflowMailbox:
    mailbox = WorkflowMailbox(task_id)  # type: ignore[arg-type]
    push_mailbox_event(mailbox, event_type, data)
    return mailbox


async def _deliver(agent) -> tuple[TaskResultData, dict]:
    """走完整链路：排空 mailbox → 总线 → 消费方，返回 (投递载荷, 模型上下文)。

    这是本文件的主力入口——**不**手工构造 ctx，ctx 一律由真实
    `_handle_task_result` 从真实 `TaskResultData` 派生。
    """
    from core.agent.agent import Agent

    await Agent.drain_mailboxes_once(agent)
    payload = await _next_payload(agent)
    await Agent._handle_task_result(agent, payload)
    return payload, agent._cognitive.last_ctx


def _wake_up(ctx: dict) -> str:
    """模型上下文里的最后一步：唤醒消息（真实静态方法）。"""
    return CognitiveLoop._build_wake_up_message(ctx)


def _cognitive_input(ctx: dict) -> str:
    """认知循环真正当作 user_message 用的那条文本（`_prepare_think_input`）。

    只补 `_prepare_think_input` 会碰到的两个字段（`_engine` / `_generation_ctx`）——
    本用例验的是任务结果分支的取值，不需要完整 CognitiveLoop 启动。
    """
    loop = object.__new__(CognitiveLoop)
    loop._engine = None
    loop._generation_ctx = None
    return loop._prepare_think_input("", "", None, True, ctx)


# ══════════════════════════════════════════════════════════════════
# U1 — completed 必须是 success 语义
# ══════════════════════════════════════════════════════════════════

class TestCompletedOutcome:
    """U1：成功任务 → success 语义。

    **本类不是"修复前即为绿"的 regression guard**——它同样断言
    `task_user_outcome` 存在（修复前该符号不存在），所以修复前是**红**的。
    它真正充当的回归网是**成功分支的措辞**：修复后该分支逐字未改（结尾问句
    仍含"汇报/稍后"，既有 `test_phase3.py::TestWakeUpMessage` 亦未动）。
    """

    @pytest.mark.asyncio
    async def test_completed_is_success_semantics_end_to_end(self):
        agent = _stub_agent(tasks={"wf-1": ("查天气", _mailbox(
            "wf-1", WorkflowEventType.COMPLETED, {"result": "明天晴天"}))})

        payload, ctx = await _deliver(agent)
        message = _wake_up(ctx)

        assert payload.is_error is False
        assert _user_outcome(ctx) == COMPLETED
        assert _semantic(message) == COMPLETED, (
            f"成功任务的唤醒消息必须宣称成功，实得: {message!r}")
        assert "查天气" in message and "明天晴天" in message, (
            "原实现的两条有效信息（意图 + 结果）不得在修复中丢失")
        assert _cognitive_input(ctx) == message, (
            "认知循环当作 user_message 的必须正是这条唤醒消息——否则测的不是同一段文本")


# ══════════════════════════════════════════════════════════════════
# U2 / U3 — failed 必须是 failure 语义（不得说成"已完成"）
# ══════════════════════════════════════════════════════════════════

class TestFailureOutcome:
    """U2/U3：规划失败与执行失败都必须落到 failure 语义。

    这是本批要修的主缺陷：修复前两条都会输出"请自然地告知用户任务已完成"。
    """

    @pytest.mark.asyncio
    async def test_planning_failure_is_not_reported_as_completed(self):
        agent = _stub_agent(tasks={"wf-1": ("写季度报告", _mailbox(
            "wf-1", WorkflowEventType.FAILED,
            {"error": "规划失败: LLM 连续返回不可解析的 plan",
             "error_type": "planning_failed"}))})

        payload, ctx = await _deliver(agent)
        message = _wake_up(ctx)

        assert payload.is_error is True
        assert _user_outcome(ctx) == FAILED
        assert not _claims_success(message), (
            f"规划失败的终态被描述成了成功——这正是 Batch 1.8 的原缺陷: {message!r}")
        assert _semantic(message) == FAILED, (
            f"规划失败的唤醒消息必须宣称失败，实得: {message!r}")

    @pytest.mark.asyncio
    async def test_execution_failure_is_not_reported_as_completed(self):
        agent = _stub_agent(tasks={"wf-1": ("跑全库扫描", _mailbox(
            "wf-1", WorkflowEventType.FAILED,
            {"error": "执行失败: 工具崩了", "error_type": "execution_failed"}))})

        payload, ctx = await _deliver(agent)
        message = _wake_up(ctx)

        assert payload.is_error is True
        assert _user_outcome(ctx) == FAILED
        assert not _claims_success(message), (
            f"执行失败的终态被描述成了成功: {message!r}")
        assert _semantic(message) == FAILED

    @pytest.mark.asyncio
    async def test_failure_without_error_detail_still_fails(self):
        """C1：failed 终态**缺 error 字段**（可选细节缺失）→ 仍是失败。

        细节缺失只能降级措辞，不能降级**事实**。
        """
        agent = _stub_agent(tasks={"wf-1": ("没细节的失败", _mailbox(
            "wf-1", WorkflowEventType.FAILED, {}))})

        _, ctx = await _deliver(agent)
        message = _wake_up(ctx)

        assert _user_outcome(ctx) == FAILED
        assert not _claims_success(message), (
            f"缺 error 细节的失败被说成了成功: {message!r}")
        assert _semantic(message) == FAILED

    @pytest.mark.asyncio
    async def test_unknown_error_type_still_fails(self):
        """C6：未知 / 未来新增的 error_type 不得因为"没登记"而翻成成功。

        映射必须按 **is_error / 终态** 兜底，而不是按 error_type 白名单。
        """
        agent = _stub_agent(tasks={"wf-1": ("未知失败", _mailbox(
            "wf-1", WorkflowEventType.FAILED,
            {"error": "某种没登记过的失败", "error_type": "brand_new_code_2030"}))})

        _, ctx = await _deliver(agent)
        message = _wake_up(ctx)

        assert _user_outcome(ctx) == FAILED
        assert _semantic(message) == FAILED, (
            f"未知 error_type 的失败必须仍是失败语义，实得: {message!r}")
        assert not _claims_success(message), (
            f"未知 error_type 的失败被说成了成功: {message!r}")

    @pytest.mark.asyncio
    async def test_partial_context_does_not_invert_failure(self):
        """ctx 只带 is_error（无 status：旧持久化记录的形状）→ 仍不得翻成成功。"""
        message = _wake_up({
            "original_intent": "旧记录里的任务",
            "result": "任务执行失败: 很久以前的错误",
            "is_error": True,
        })
        assert _semantic(message) == FAILED, (
            f"只有 is_error=True 的旧记录必须仍是失败语义，实得: {message!r}")
        assert not _claims_success(message), (
            f"只有 is_error=True 的旧记录被说成了成功: {message!r}")


# ══════════════════════════════════════════════════════════════════
# U4 — cancelled 必须是 cancelled 语义
# ══════════════════════════════════════════════════════════════════

class TestCancelledOutcome:
    """U4：取消的终态语义是**取消**——既不谎报成功，也不默认降级为失败。

    生产侧对取消有专属措辞（`terminal_payload` → "任务已取消: …"），所以用户级
    语义必须保住这个区分（Batch 1.8 §17：以真实代码为准，不擅自归类）。
    """

    @pytest.mark.asyncio
    async def test_cancelled_is_cancelled_not_completed(self):
        agent = _stub_agent(tasks={"wf-1": ("整理会议纪要", _mailbox(
            "wf-1", WorkflowEventType.CANCELLED, {"reason": "用户取消"}))})

        payload, ctx = await _deliver(agent)
        message = _wake_up(ctx)

        assert _user_outcome(ctx) == CANCELLED
        assert not _claims_success(message), (
            f"已取消的任务被描述成了成功: {message!r}")
        assert _semantic(message) == CANCELLED, (
            "取消终态必须保留「取消」语义（不得压成成功、也不得默认压成失败），"
            f"实得: {message!r}")

    @pytest.mark.asyncio
    async def test_cancelled_is_distinguishable_from_failed(self):
        """取消与失败的**数据面**必须可区分——`is_error` 一个布尔做不到这件事。"""
        cancelled_agent = _stub_agent(tasks={"wf-1": ("被取消的任务", _mailbox(
            "wf-1", WorkflowEventType.CANCELLED, {"reason": "用户取消"}))})
        failed_agent = _stub_agent(tasks={"wf-1": ("失败的任务", _mailbox(
            "wf-1", WorkflowEventType.FAILED, {"error": "工具崩了"}))})

        _, cancelled_ctx = await _deliver(cancelled_agent)
        _, failed_ctx = await _deliver(failed_agent)

        assert _user_outcome(cancelled_ctx) != _user_outcome(failed_ctx), (
            "取消与失败压成了同一个用户级语义——两者在 TaskResultData 上确实只差"
            "一个 is_error 布尔（Batch 1.8 §7：此时才允许扩最小稳定字段）")


# ══════════════════════════════════════════════════════════════════
# U3 — restart 补投与 live 投递语义一致
# ══════════════════════════════════════════════════════════════════

class TestRestartRecovery:
    """U3：同一个终态，live 投递与 restart 补投必须给出同一用户级语义。

    真实路径：进程 A 里终态落库（delivered=False）→ 进程"重启"（新 Agent 替身）→
    `_redeliver_pending_task_results` 补投 → 消费方 → 认知输入。
    """

    @pytest.mark.asyncio
    async def test_restart_recovery_failure_matches_live(self):
        from core.agent.agent import Agent

        repo = _StatefulRepo()
        failed = {"error": "规划失败: 无法解析", "error_type": "planning_failed"}

        live_agent = _stub_agent(
            repo=repo, tasks={"wf-1": ("写季度报告", _mailbox(
                "wf-1", WorkflowEventType.FAILED, failed))})
        _, live_ctx = await _deliver(live_agent)
        live_message = _wake_up(live_ctx)

        restarted = _stub_agent(repo=_restarted_with_same_record(repo))
        await Agent._redeliver_pending_task_results(restarted)
        payload = await _next_payload(restarted)
        await Agent._handle_task_result(restarted, payload)
        recovered_message = _wake_up(restarted._cognitive.last_ctx)

        assert _user_outcome(restarted._cognitive.last_ctx) == FAILED, (
            "重启补投把失败丢掉了——用户看到的是「上次的任务已完成」")
        assert not _claims_success(recovered_message), (
            f"重启补投后的失败被说成了成功: {recovered_message!r}")
        assert _semantic(recovered_message) == _semantic(live_message) == FAILED
        assert _tail(recovered_message) == _tail(live_message), (
            "live 与 recovery 必须经过**同一个**映射（Batch 1.8 §15）——"
            f"实得 live={live_message!r} recovery={recovered_message!r}")

    @pytest.mark.asyncio
    async def test_restart_recovery_success_matches_live(self):
        """U6：成功分支也走一遍——只修失败分支而把成功改坏同样是缺陷。"""
        from core.agent.agent import Agent

        repo = _StatefulRepo()
        live_agent = _stub_agent(
            repo=repo, tasks={"wf-1": ("查天气", _mailbox(
                "wf-1", WorkflowEventType.COMPLETED, {"result": "明天晴天"}))})
        _, live_ctx = await _deliver(live_agent)
        live_message = _wake_up(live_ctx)

        restarted = _stub_agent(repo=_restarted_with_same_record(repo))
        await Agent._redeliver_pending_task_results(restarted)
        payload = await _next_payload(restarted)
        await Agent._handle_task_result(restarted, payload)
        recovered_message = _wake_up(restarted._cognitive.last_ctx)

        assert _user_outcome(restarted._cognitive.last_ctx) == COMPLETED
        assert _semantic(recovered_message) == COMPLETED
        assert _tail(recovered_message) == _tail(live_message)

    @pytest.mark.asyncio
    async def test_cancelled_survives_restart_as_cancelled(self):
        from core.agent.agent import Agent

        repo = _StatefulRepo()
        agent = _stub_agent(repo=repo, tasks={"wf-1": ("整理纪要", _mailbox(
            "wf-1", WorkflowEventType.CANCELLED, {"reason": "用户取消"}))})
        await _deliver(agent)

        restarted = _stub_agent(repo=_restarted_with_same_record(repo))
        await Agent._redeliver_pending_task_results(restarted)
        payload = await _next_payload(restarted)
        await Agent._handle_task_result(restarted, payload)

        assert _user_outcome(restarted._cognitive.last_ctx) == CANCELLED, (
            "取消语义没能跨重启存活")
        recovered_message = _wake_up(restarted._cognitive.last_ctx)
        assert _semantic(recovered_message) == CANCELLED, (
            f"重启补投后的取消终态语义不对，实得: {recovered_message!r}")
        assert not _claims_success(recovered_message)

    @pytest.mark.asyncio
    async def test_record_without_status_falls_back_to_is_error(self):
        """C3：**缺 `status` 的记录**（外力改写 / 异构写入方）仍按 `is_error` 兜底。

        **这不是"旧版本记录"的兼容性测试**——`background_task_results` 从建集合那次
        提交起，`$set` 里就一直带 `status`（`git log -S '"status": status' --
        core/storage/background_task_repo.py` 只有一次提交），本仓没有任何写入方能
        产出缺 `status` 的记录。所以这条钉的是**纵深防御**：字段缺失时语义只能降级
        到 `is_error`，而那个方向**必须**是"失败仍是失败"。
        """
        from core.agent.agent import Agent

        repo = _StubRepo(pending=[{
            "task_id": "wf-old",
            "result": "任务执行失败: 上次进程里就失败了",
            "original_intent": "字段被外力改写的任务",
            "dispatched_at": _DISPATCHED_AT,
            "is_error": True,
            # 刻意不带 status：模拟被改写 / 异构写入方的文档
        }])
        agent = _stub_agent(repo=repo)

        await Agent._redeliver_pending_task_results(agent)
        payload = await _next_payload(agent)
        await Agent._handle_task_result(agent, payload)
        message = _wake_up(agent._cognitive.last_ctx)

        assert _user_outcome(agent._cognitive.last_ctx) == FAILED, (
            "缺 status 的记录其失败被翻转成了成功——兜底方向错了")
        assert _semantic(message) == FAILED, (
            f"缺 status 的记录必须是失败语义，实得: {message!r}")
        assert not _claims_success(message), (
            f"缺 status 的记录的失败被说成了成功: {message!r}")

    @pytest.mark.asyncio
    async def test_malformed_record_does_not_brick_the_redelivery_pass(self):
        """Challenger D1：一条坏文档不得中断整轮补投，更不得让 Agent 起不来。

        `doc.get(k, default)` 在"键存在但值为 null"时返回 **None**（不是 default），
        pydantic 随即抛 ValidationError。修复前它冒泡出启动步骤循环 →
        `lifecycle.start()` 的 BaseException 回滚 → **一条坏文档让整个 Agent 起不来**，
        且排在它后面的正常记录一条都补投不出去（用户的那条终态静默消失）。
        """
        from core.agent.agent import Agent

        repo = _StubRepo(pending=[
            {"task_id": "wf-bad", "result": None, "original_intent": "坏记录",
             "dispatched_at": _DISPATCHED_AT, "is_error": True, "status": "failed"},
            {"task_id": "wf-ok", "result": "任务执行失败: 正常的失败记录",
             "original_intent": "好记录", "dispatched_at": _DISPATCHED_AT,
             "is_error": True, "status": "failed"},
        ])
        agent = _stub_agent(repo=repo)

        await Agent._redeliver_pending_task_results(agent)   # 修复前：这里抛 ValidationError

        payload = await _next_payload(agent)
        assert str(payload.task_id) == "wf-ok", (
            "坏文档之后的正常记录必须照常补投（否则用户永久失去那条终态）")
        assert repo.delivered == [], "补投只入队——确认仍由消费侧授予"

    @pytest.mark.asyncio
    async def test_interrupted_after_restart_reads_as_failure(self):
        """§17：`interrupted` 是**停机兜底写下的终态记录**（未产出结果），
        重启补投后对用户是"没做成"，绝不能是成功。"""
        from core.agent.agent import Agent

        repo = _StatefulRepo()
        agent = _stub_agent(repo=repo, tasks={"wf-1": ("跑到一半被停机", _mailbox(
            "wf-1", WorkflowEventType.QUEUED, {"task_id": "wf-1"}))})
        await Agent._stop_mailbox(agent)          # 无终态 → 落库 interrupted
        assert [w["status"] for w in repo.written] == ["interrupted"]

        restarted = _stub_agent(repo=repo)
        await Agent._redeliver_pending_task_results(restarted)
        payload = await _next_payload(restarted)
        await Agent._handle_task_result(restarted, payload)
        message = _wake_up(restarted._cognitive.last_ctx)

        assert _user_outcome(restarted._cognitive.last_ctx) == FAILED
        assert _semantic(message) == FAILED, (
            f"被停机打断的任务必须读成失败语义，实得: {message!r}")
        assert not _claims_success(message), (
            f"被停机打断的任务被说成了成功: {message!r}")


# ══════════════════════════════════════════════════════════════════
# U4 — 同一条终态重复投递不得改变语义
# ══════════════════════════════════════════════════════════════════

class TestDuplicateReplay:
    """U4（at-least-once）：宁可重复告知，但两次的**语义**必须一致。"""

    @pytest.mark.asyncio
    async def test_duplicate_delivery_keeps_same_outcome(self):
        from core.agent.agent import Agent

        repo = _StatefulRepo()
        agent = _stub_agent(repo=repo, tasks={"wf-1": ("写报告", _mailbox(
            "wf-1", WorkflowEventType.FAILED, {"error": "工具崩了"}))})
        payload, _ = await _deliver(agent)

        # 模拟"投递确认没落库就崩了"→ 下次启动重复投递同一条
        repo.written[0]["delivered"] = False
        restarted = _stub_agent(repo=repo)
        await Agent._redeliver_pending_task_results(restarted)
        second = await _next_payload(restarted)
        await Agent._handle_task_result(restarted, second)

        first_message = _wake_up(payload.model_dump())
        second_message = _wake_up(restarted._cognitive.last_ctx)

        assert _semantic(first_message) == _semantic(second_message) == FAILED, (
            "同一终态的两次投递给出了不同语义——at-least-once 只允许重复，"
            f"不允许改变含义。第一次={first_message!r} 第二次={second_message!r}")
        assert _user_outcome(restarted._cognitive.last_ctx) == FAILED


# ══════════════════════════════════════════════════════════════════
# §26 — 顺序扰动：三种终态互不串台
# ══════════════════════════════════════════════════════════════════

class TestOrderPerturbation:
    """同一进程内连续处理多种终态，语义不得残留 / 串台（§26）。"""

    async def _run(self, order: list[str]) -> list[str]:
        """按 `order` 一次性投递多种终态，返回按投递顺序归位的用户级语义。"""
        from core.agent.agent import Agent

        agent = _stub_agent(tasks={
            f"wf-{kind}": (f"任务-{kind}", _mailbox(f"wf-{kind}", *_TERMINAL_EVENTS[kind]))
            for kind in order
        })
        await Agent.drain_mailboxes_once(agent)

        seen: list[str] = []
        for _ in order:
            payload = await _next_payload(agent)
            await Agent._handle_task_result(agent, payload)
            ctx = agent._cognitive.last_ctx
            outcome = _user_outcome(ctx)
            assert _semantic(_wake_up(ctx)) == outcome, (
                f"消息语义与数据面语义不一致（task_id={payload.task_id}）")
            seen.append(outcome)
        return seen

    @pytest.mark.asyncio
    async def test_success_then_failure_then_cancel(self):
        seen = await self._run([COMPLETED, FAILED, CANCELLED])
        assert seen == [COMPLETED, FAILED, CANCELLED], (
            f"顺序扰动下语义串台了: {seen}")

    @pytest.mark.asyncio
    async def test_cancel_then_failure_then_success(self):
        seen = await self._run([CANCELLED, FAILED, COMPLETED])
        assert seen == [CANCELLED, FAILED, COMPLETED], (
            f"逆序扰动下语义串台了: {seen}")


# ══════════════════════════════════════════════════════════════════
# §6/§7 — TaskResultData 是否真的能表达三种终态
# ══════════════════════════════════════════════════════════════════

class TestTaskResultDataCarriesOutcome:
    """数据面：投递载荷必须**无损**表达三种用户级终态。"""

    @pytest.mark.asyncio
    async def test_live_payload_carries_terminal_status(self):
        for event_type, data, expected in (
            (WorkflowEventType.COMPLETED, {"result": "好"}, COMPLETED),
            (WorkflowEventType.FAILED, {"error": "崩了"}, FAILED),
            (WorkflowEventType.CANCELLED, {"reason": "用户取消"}, CANCELLED),
        ):
            agent = _stub_agent(tasks={"wf-1": ("任务", _mailbox(
                "wf-1", event_type, data))})
            payload, _ = await _deliver(agent)

            assert getattr(payload, "status", None) == expected, (
                f"{event_type.value} 终态在投递载荷上没有留下终态标识"
                "（只有一个 is_error 布尔：failed 与 cancelled 无法区分）——"
                "措辞层于是只能靠猜，这正是本批的根因")

    @pytest.mark.asyncio
    async def test_persisted_status_matches_redelivered_status(self):
        """落库的 status 与补投后投递载荷上的 status 必须是同一个值。"""
        from core.agent.agent import Agent

        repo = _StatefulRepo()
        agent = _stub_agent(repo=repo, tasks={"wf-1": ("任务", _mailbox(
            "wf-1", WorkflowEventType.CANCELLED, {"reason": "用户取消"}))})
        await _deliver(agent)
        assert repo.written[0]["status"] == CANCELLED

        restarted = _stub_agent(repo=_restarted_with_same_record(repo))
        await Agent._redeliver_pending_task_results(restarted)
        payload = await _next_payload(restarted)

        assert getattr(payload, "status", None) == CANCELLED, (
            "补投路径把落库时记下的终态丢了——恢复 truth 与持久化 truth 不一致")


# ══════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════

def _tail(message: str) -> str:
    """去掉开头的时间前缀——两次调用之间可能跨秒，时间戳不参与语义比较。"""
    return message.split("。", 1)[1] if "。" in message else message
