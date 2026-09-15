"""工具重试的幂等性边界 — CODE_REVIEW_REPORT #46 / gap row #10。

## 为什么需要这个文件

`core/tool_runtime/scheduler.py::_execute_tool` 会重试失败的工具调用（`max_retries`
默认 2，`core/tool_runtime/task.py:69`）。既有 `test_task_scheduler.py` 只断言
"重试确实发生了"（`call_count == 2`，即**副作用发生了两次**），既没有断言
"不可重试的错误真的不重试"，也没有把"重试会重复副作用"这条现实显式写下来——
本文件把两侧边界都钉住。

## 契约（逐条对应源码，勿凭印象改）

1. **重试用同一个 `call_id` 再 invoke**：`dispatch_tool_calls` 把 LLM 的
   `tool_call_id` 放进 payload（`scheduler.py:264`），`_execute_tool` 在重试循环
   **之外**只读一次（`scheduler.py:586`），循环内每次
   `clone_for_call(call_id=call_id)`（`scheduler.py:596`）——于是第 1 次与第 N 次
   尝试收到的 `ctx.call_id` **逐字相同**（ctx 本身是每次新建的副本）。
2. **没有任何按 `call_id` 的去重/幂等存储**：`_tasks` 的键是每次新建的 `TaskId`
   （`task.py:153` `new_task_id()`），同参调用不会被识别为重复；
   `core/storage/idempotency.py` 的键是 `(conversation_id, client_request_id)`，
   只在 v2 WS 入口给**用户消息**去重（`core/input/ws_v2.py`），工具路径不消费它。
3. **重试判定有两个来源（2026-09-16 修复后，勿混淆）**：
   - 插件**自己返回**的 `error_type` 是**插件的陈述**：可重试集合仍由
     `ErrorType.is_retryable`（TIMEOUT / RATE_LIMITED / NETWORK_ERROR）决定，
     与 `SafetyProfile.idempotent` **无关**；`ToolResult.fail` 的 `error_type`
     缺省是 `None` → 不带类型的失败**不重试**。
   - 调度器**自己合成**的超时（`_execute_tool` 的 `asyncio.TimeoutError` 分支把它
     写成 `ErrorType.TIMEOUT`）是**调度器的猜测**：那个标签不是插件说的，所以
     **只有**插件声明 `SafetyProfile.idempotent=True` 才重试。默认 `False`
     = fail-safe（忘记声明 → 少重试一次，而不是把副作用做两遍）。
4. **重试安全的声明面**是 `SafetyProfile.idempotent`（"重复调用不产生额外副作用"，
   只读/可安全重入）；调度器经 `executor.declares_idempotent(tool_name)` 询问
   （与 `declared_request_timeout` 同一委托链，**不使用 getattr 兜底**）。

因此：**插件自己声明可重试的失败，第一次尝试已落地的副作用仍会在重试里再落地
一次**（§一、§二）；调度器合成的超时只在插件声明幂等时才重试（§五）。
本文件把这两个现实都显式断言出来，而不是断言一个代码并未提供的保证。
"""

from __future__ import annotations

import asyncio
from dataclasses import fields
from typing import Any

import pytest

from Module.tool.protocol import ErrorType, ToolPlugin, ToolResult
from Module.tool.safety import SafetyProfile
from core.adapter import ToolAdapter, ToolContext
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId
from core.tool_runtime import QueuedToolCall, QueuedToolCallState, ToolCallDispatcher
from core.tool_runtime import scheduler as _scheduler_mod

# 期望值**现算**而不是写死：枚举加了新成员、或 `is_retryable` 改了分类，
# 本文件的期望调用次数会自动跟着变（断言"某个错误类型不可重试"的用例必须这样写，
# 否则改坏分类的那一刻用例就永久失真）。
_RETRYABLE_TYPES: tuple[ErrorType, ...] = tuple(e for e in ErrorType if e.is_retryable)
_NON_RETRYABLE_TYPES: tuple[ErrorType, ...] = tuple(e for e in ErrorType if not e.is_retryable)

# 真实管道路径上抽样覆盖的分类（全枚举矩阵在 `_ScriptedExecutor` 用例里跑）。
_PIPELINE_TYPES: tuple[ErrorType, ...] = (
    ErrorType.NETWORK_ERROR,   # 可重试（真插件 web 抓取失败走这条）
    ErrorType.TIMEOUT,         # 可重试
    ErrorType.TOOL_ERROR,      # 不可重试（插件内部异常经 safety 管道归一到这里）
    ErrorType.PERMISSION_DENIED,
)


def _tc(id_: str = "c1", name: str = "tool", args: str = "{}") -> ToolCall:
    """构造一个 ToolCall（`id` 即 LLM 的 tool_call_id，会线程化为 ctx.call_id）。"""
    return ToolCall(
        id=ToolCallId(id_),
        function=ToolCallFunction(name=name, arguments=args),
    )


def _expected_invocations(error_type: ErrorType | None, max_retries: int) -> int:
    """持续失败时：可重试 → 1 次首发 + max_retries 次重试；否则只有首发。"""
    retryable = error_type is not None and error_type.is_retryable
    return 1 + (max_retries if retryable else 0)


def _expected_calls_until_success(error_type: ErrorType | None) -> int:
    """第 1 次失败、之后成功时：可重试 → 2 次；不可重试 → 只有 1 次（不再进插件）。"""
    retryable = error_type is not None and error_type.is_retryable
    return 2 if retryable else 1


@pytest.fixture
def fast_backoff(monkeypatch):
    """把重试退避压到 0s——本文件断言的是**重试的次数与语义**，不是退避曲线。

    生产退避曲线（`2^n`，cap 30s）由 `core/tool_runtime/tests/test_task_model.py`
    的 `test_retry_backoff_first/second/third/capped` 独立锁死，退避与
    `_CALL_BUDGET` 的一致性由 `test_task_scheduler.py::test_dispatch_budget_never_below_single_call_budget`
    锁死——此处中性化只是为了不让每条用例白等 2s+4s（默认 max_retries=2）。
    """
    monkeypatch.setattr(QueuedToolCall, "retry_backoff_seconds", lambda self: 0.0)
    return 0.0


class _Attempt:
    """脚本项：一次尝试的结局（成功 / 带或不带 `error_type` 的失败）。

    把 `error_type=None` 与"成功"分开表示——`ToolResult.fail(error_type=None)`
    是插件失败路径的**缺省**形态（`Module/tool/protocol.py:630-638`），
    必须能被单独脚本化，否则测不出"不带类型 → 不重试"。
    """

    __slots__ = ("success", "error_type")

    def __init__(self, *, success: bool = False, error_type: ErrorType | None = None) -> None:
        self.success = success
        self.error_type = error_type


_OK = _Attempt(success=True)
_FAIL_NO_TYPE = _Attempt()                       # ToolResult.fail 缺省 error_type=None


def _fail(error_type: ErrorType) -> _Attempt:
    """带类型的失败（可重试与否由 `ErrorType.is_retryable` 决定）。"""
    return _Attempt(error_type=error_type)


class _ScriptedExecutor:
    """按脚本逐次返回结果的假 executor（鸭子类型满足调度器的两项依赖）。

    - ``declared_request_timeout()`` → None：不声明插件预算，调度器用 `_TOOL_TIMEOUT`；
    - ``declares_idempotent()`` → 构造参数（默认 False）：模拟
      `SafetyProfile.idempotent` 的**插件声明**；
    - ``execute()``：每次进入都记一条 ``ctx.call_id`` 与 ctx 对象自身，并落一次
      **不可回滚的副作用**（`side_effects`）——用来演示"第一次尝试已落地的东西
      会不会在重试里再落一次"。
    """

    def __init__(self, script: list[_Attempt], *, idempotent: bool = False) -> None:
        self._script = list(script)
        self._idempotent = idempotent
        self.call_ids: list[str] = []
        self.contexts: list[ToolContext] = []
        self.side_effects: list[str] = []

    def declared_request_timeout(self, tool_name: str) -> float | None:
        return None

    def declares_idempotent(self, tool_name: str) -> bool:
        """模拟插件声明（默认 False = 未声明，与 `SafetyProfile` 缺省一致）。"""
        return self._idempotent

    async def execute(self, tool_name: str, *, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        attempt = len(self.call_ids) + 1
        self.call_ids.append(ctx.call_id)
        self.contexts.append(ctx)
        self.side_effects.append(f"effect#{attempt}")
        step = self._script[min(attempt, len(self._script)) - 1]
        if step.success:
            return ToolResult.ok(name=tool_name, content="ok", duration_ms=0.0)
        return ToolResult.fail(
            name=tool_name, error=f"attempt#{attempt} failed", error_type=step.error_type)


class _HangingThenOkExecutor:
    """第 1 次调用挂在永不 set 的 Event 上（由 `wait_for` 超时取消），第 2 次成功。

    内层 `wait_for(..., timeout=5.0)` 只是**失败保险**：若调度器的单次超时没生效，
    用例在 5s 后以 TOOL_ERROR（不可重试）失败，而不是把整个测试会话挂死。

    `idempotent` 即"插件声明"：True 时调度器重试自己合成的超时，False（默认）时不重试
    ——两个方向都由 `test_synthesised_timeout_*` 用例锁死。
    """

    def __init__(self, *, idempotent: bool = False) -> None:
        self.attempts = 0
        self.cancelled_attempts = 0
        self.call_ids: list[str] = []
        self._idempotent = idempotent
        self._release = asyncio.Event()

    def declared_request_timeout(self, tool_name: str) -> float | None:
        return None

    def declares_idempotent(self, tool_name: str) -> bool:
        return self._idempotent

    async def execute(self, tool_name: str, *, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        self.attempts += 1
        self.call_ids.append(ctx.call_id)
        if self.attempts == 1:
            try:
                await asyncio.wait_for(self._release.wait(), timeout=5.0)
            except asyncio.CancelledError:
                self.cancelled_attempts += 1
                raise
            raise AssertionError("第 1 次尝试未被单次超时取消——超时路径没有生效")
        return ToolResult.ok(name=tool_name, content="ok", duration_ms=0.0)


def _flaky_plugin_class(error_type: ErrorType | None, seen: list[str], *,
                        idempotent: bool = False):
    """构造一个"第 1 次失败、之后成功"的真实插件类（经完整 safety 管道执行）。

    `seen` 记录每次 execute 收到的 `ctx.call_id`——计数放在闭包里，
    避免穿透 ToolAdapter 的私有注册表。`idempotent` 落到 `SafetyProfile`：
    插件**返回**的错误不受它影响（§二有意用 idempotent=False 的插件验证这一点）。
    """

    class _Flaky(ToolPlugin):
        name = "flaky"
        description = "fails once then succeeds"
        param_model = None
        capabilities = set()
        safety = SafetyProfile(idempotent=idempotent)

        async def execute(self, *, ctx: ToolContext, emit=None, params=None) -> ToolResult:
            seen.append(ctx.call_id)
            if len(seen) == 1:
                return ToolResult.fail(
                    name="flaky", error="首次失败", error_type=error_type)
            return ToolResult.ok(name="flaky", content="ok", duration_ms=0.0)

    return _Flaky


def _hanging_plugin_class(*, idempotent: bool, seen: list[int], cancelled: list[int]):
    """构造一个"第 1 次挂起（被超时取消）、第 2 次成功"的**真实**插件类。

    与 `_HangingThenOkExecutor` 的区别：这里走完整 `ToolAdapter` → `SafetyPipeline`
    管道，因此 `SafetyProfile.idempotent` 经 `ToolAdapter.declares_idempotent`
    → `ToolManager.declares_idempotent` 的**生产委托链**被问到（这正是本修复要接上
    的那一环）。`request_timeout` 保持默认 30s：调度器的单次超时由调用方压到 0.05s，
    于是先触发的是**调度器自己**那条 `asyncio.TimeoutError` 分支，而不是
    `SafetyPipeline` 内层的超时（后者同样合成 TIMEOUT，但它是"返回给调度器的
    ToolResult"，不在本次修复范围）。
    """

    class _Hanging(ToolPlugin):
        name = "hanging_real"
        description = "hangs on the first attempt, then succeeds"
        param_model = None
        capabilities = set()
        safety = SafetyProfile(idempotent=idempotent)

        async def execute(self, *, ctx: ToolContext, emit=None, params=None) -> ToolResult:
            seen.append(1)
            if len(seen) == 1:
                try:
                    await asyncio.wait_for(asyncio.Event().wait(), timeout=5.0)
                except asyncio.CancelledError:
                    cancelled.append(1)
                    raise
                raise AssertionError("第 1 次尝试未被单次超时取消——超时路径没有生效")
            return ToolResult.ok(name="hanging_real", content="ok", duration_ms=0.0)

    return _Hanging


class _ShrunkBudgetAdapter:
    """把真实 `ToolAdapter` 的单次预算压小的代理（只为让用例跑得快）。

    只改 `declared_request_timeout`（恒 None → 调度器用被 monkeypatch 的
    `_TOOL_TIMEOUT`）；`execute()` 与 `declares_idempotent()` **原样走真实
    `ToolAdapter`**，所以"插件声明 → manager → adapter"的委托链仍是真的。
    """

    def __init__(self, adapter: ToolAdapter) -> None:
        self._adapter = adapter

    def declared_request_timeout(self, tool_name: str) -> float | None:
        return None

    def declares_idempotent(self, tool_name: str) -> bool:
        return self._adapter.declares_idempotent(tool_name)

    async def execute(self, tool_name: str, *, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        return await self._adapter.execute(tool_name, ctx=ctx, **kwargs)


# ═══════════════════════════════════════════════════════════════════
# 一、重试安全边界：同一个 call_id 再 invoke，副作用再落一次
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
async def test_retry_reenters_tool_with_same_call_id_and_duplicates_side_effect(fast_backoff):
    """重试 = 用**同一个** `call_id` 再 invoke 一次 → 副作用会再落地一次。

    断言的是当前系统的**真实保证**（"重试安全由插件自己负责"），不是"重试幂等"：
    - 两次尝试收到的 `ctx.call_id` 完全相同（`scheduler.py:586` 在重试循环外读一次，
      `scheduler.py:596` 每次 clone 都用它）；
    - 每次尝试拿到的是**新的 ctx 副本**（`clone_for_call`），所以插件不能靠
      "ctx 变了"来判断这是不是同一个逻辑调用；
    - 副作用清单为 2 条——调度层没有任何按 `call_id` 去重的存储。

    **本用例走的是插件自己返回的 `NETWORK_ERROR`**（插件的陈述）——2026-09-16 起
    调度器合成的超时要插件声明 `idempotent=True` 才重试（§五），但插件自己声明的
    可重试错误照旧重试，所以本用例的期望**不变**。

    若哪天调度层真的加了按 call_id 的去重，本用例会红；届时该改的是契约文档
    与本文件的期望，而不是把断言删掉。
    """
    executor = _ScriptedExecutor([_fail(ErrorType.NETWORK_ERROR), _OK])
    sched = ToolCallDispatcher(tool_executor=executor, num_workers=1, checkpoint_path=None)
    await sched.start()
    try:
        results = await sched.dispatch_tool_calls(
            "req-1", [_tc("tc_side_effect", "side_effect_tool", "{}")])
    finally:
        await sched.stop()

    assert len(results) == 1
    _call, result = results[0]
    assert result.success, "第 2 次尝试应成功"

    # ① 两次进入插件（= 副作用两次）
    assert executor.side_effects == ["effect#1", "effect#2"], (
        "重试必须**再进一次插件**——这正是副作用重复的来源")
    # ② 两次尝试的 call_id 逐字相同
    assert executor.call_ids == ["tc_side_effect", "tc_side_effect"], (
        "重试不得更换 call_id：scheduler.py:586 在循环外读一次，循环内复用")
    # ③ 但 ctx 是每次新建的副本（clone_for_call）——插件不能靠 ctx 身份判重
    assert executor.contexts[0] is not executor.contexts[1]


@pytest.mark.service
@pytest.mark.asyncio
async def test_pipeline_retry_safety_is_plugin_responsibility(fast_backoff):
    """真实 safety 管道上同样如此：插件被进入两次，且两次的 `ctx.call_id` 相同。

    这条走真实 `ToolAdapter`（参数校验 + safety 管道 + ToolManager 在途句柄表），
    证明"重复副作用"不是假 executor 的产物，而是生产路径的行为。
    """
    seen: list[str] = []
    tm = ToolAdapter([_flaky_plugin_class(ErrorType.NETWORK_ERROR, seen)], session_id="test")
    await tm.start()

    sched = ToolCallDispatcher(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()
    try:
        results = await sched.dispatch_tool_calls("req-1", [_tc("tc_real", "flaky", "{}")])
    finally:
        await sched.stop()

    assert results[0][1].success
    assert seen == ["tc_real", "tc_real"], (
        "真实管道上重试也必须携带同一个 call_id 再入插件")


# ═══════════════════════════════════════════════════════════════════
# 二、不可重试的错误：恰好调用一次（两个方向都断言）
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attempt",
    [pytest.param(_fail(e), id=e.value) for e in ErrorType]
    + [pytest.param(_FAIL_NO_TYPE, id="no_error_type")],
)
async def test_invocation_count_matches_error_type_retryability(fast_backoff, attempt):
    """调用次数 == `1 + max_retries * is_retryable`——两个方向都断言。

    既有用例只覆盖了"可重试 → 2 次"这一个方向；本用例对 `ErrorType` 全枚举
    （外加 `error_type=None`，即插件用 `ToolResult.fail` 缺省值）断言：
    - 可重试（TIMEOUT/RATE_LIMITED/NETWORK_ERROR）→ 首发 + max_retries 次重试；
    - 不可重试 / 不带 error_type → **恰好 1 次**，且 `retry_count` 保持 0
      （gate 在 `scheduler.py:630-631` 直接 return，不进入退避）。
    """
    max_retries = 2
    executor = _ScriptedExecutor([attempt])
    sched = ToolCallDispatcher(tool_executor=executor, num_workers=1, checkpoint_path=None)
    await sched.start()

    t = QueuedToolCall.new_tool_call("req-1", "always_fail", {})
    t.max_retries = max_retries
    fut = t.get_future()
    try:
        await sched.submit(t)
        result = await asyncio.wait_for(fut, timeout=2.0)
    finally:
        await sched.stop()

    expected = _expected_invocations(attempt.error_type, max_retries)
    assert len(executor.call_ids) == expected, (
        f"error_type={attempt.error_type} 的调用次数应为 {expected}"
        f"（is_retryable={attempt.error_type is not None and attempt.error_type.is_retryable}）")
    assert t.retry_count == (max_retries if expected > 1 else 0)
    assert not result.success, "脚本里没有成功项，结果必须是失败"
    assert result.error_type is attempt.error_type


@pytest.mark.service
@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", _PIPELINE_TYPES, ids=lambda e: e.value)
async def test_plugin_declared_error_type_decides_retry_through_real_pipeline(
        fast_backoff, error_type):
    """插件返回的 `error_type` 直接决定重试——真实管道上同样成立。

    可重试类型：第 1 次失败 → 第 2 次成功（`call_id` 相同）；
    不可重试类型：**恰好 1 次**，失败结果原样上抛给调用方。
    """
    seen: list[str] = []
    tm = ToolAdapter([_flaky_plugin_class(error_type, seen)], session_id="test")
    await tm.start()

    sched = ToolCallDispatcher(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()
    try:
        results = await sched.dispatch_tool_calls("req-1", [_tc("tc_pipe", "flaky", "{}")])
    finally:
        await sched.stop()

    _call, result = results[0]
    expected = _expected_calls_until_success(error_type)
    assert len(seen) == expected
    assert seen == ["tc_pipe"] * expected
    if error_type.is_retryable:
        assert result.success and result.error_type is None
    else:
        assert not result.success and result.error_type is error_type


# ═══════════════════════════════════════════════════════════════════
# 三、重试耗尽：终态、错误上抛、不再有第 N+1 次调用
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
async def test_retry_exhaustion_is_terminal_and_error_surfaces(fast_backoff):
    """重试耗尽 → 终态**唯一**、错误上抛给调用方、此后不再有任何调用。

    `max_retries` 取生产默认值 2（`task.py:69`）→ 恰好 3 次尝试。注意
    `_worker_loop` 把**工具失败**记成 `QueuedToolCallState.COMPLETED`
    （`scheduler.py:503-507`：`_execute_tool` 是"返回失败的 ToolResult"而非抛异常），
    所以终态是 COMPLETED + `result.success=False`，不是 FAILED。
    """
    executor = _ScriptedExecutor([_fail(ErrorType.NETWORK_ERROR)])
    sched = ToolCallDispatcher(tool_executor=executor, num_workers=1, checkpoint_path=None)
    await sched.start()

    t = QueuedToolCall.new_tool_call("req-1", "always_fail", {})
    assert t.max_retries == 2, "生产默认重试上限变化了——本用例的期望次数需同步重算"
    fut = t.get_future()
    try:
        await sched.submit(t)
        # ① 终态经 future 落定（submit → future 与 dispatch 是同一收口路径）
        result = await asyncio.wait_for(fut, timeout=2.0)
        # ② 错误原样上抛给调用方（LLM 侧看到的是可读的失败）
        assert not result.success
        assert result.error_type is ErrorType.NETWORK_ERROR
        assert "attempt#3 failed" in (result.error or "")
        assert "always_fail" in result.llm_content
        # ③ 终态与计数：首发 + 2 次重试后用尽
        assert t.retry_count == t.max_retries == 2
        assert t.is_terminal
        assert t.status == QueuedToolCallState.COMPLETED  # 失败结果是 COMPLETED+success=False
        # ④ 不再有第 4 次调用（有界观察：退避已压到 0，50ms 足够让"多试一次"的回归跑完）
        await asyncio.sleep(0.05)
        assert len(executor.call_ids) == 3, "终态之后仍在继续调用工具"
        assert sched.running_count == 0
        assert sched.pending_count == 0
        # ⑤ 终态任务已从注册表清理（worker 在 resolve future 后同步 pop）
        assert sched.get_task(t.id) is None
    finally:
        await sched.stop()

    # ⑥ dispatch 主路径同样把失败如实交回（不吞成空错误）
    executor2 = _ScriptedExecutor([_fail(ErrorType.NETWORK_ERROR)])
    sched2 = ToolCallDispatcher(tool_executor=executor2, num_workers=1, checkpoint_path=None)
    await sched2.start()
    try:
        results = await sched2.dispatch_tool_calls("req-2", [_tc("tc_exhaust", "always_fail", "{}")])
    finally:
        await sched2.stop()
    assert len(results) == 1
    assert not results[0][1].success
    assert results[0][1].error_type is ErrorType.NETWORK_ERROR
    assert len(executor2.call_ids) == 3


@pytest.mark.service
@pytest.mark.asyncio
async def test_cancel_during_retry_backoff_stops_further_invocation(monkeypatch):
    """重试退避期间的取消 → 不再有下一次调用，future 以 CancelledError 收口。

    退避 sleep 就在 `_execute_tool` 的重试循环里（`scheduler.py:640`），
    `cancel()` 取消 worker task 会打断它（`scheduler.py:398-402`）——这条路径
    在既有用例里没有覆盖（既有取消用例都停在"执行中被取消"）。
    """
    # 5s 退避只是让"已进入退避"这个窗口可观测；cancel 会立刻打断它，不会真等。
    monkeypatch.setattr(QueuedToolCall, "retry_backoff_seconds", lambda self: 5.0)
    executor = _ScriptedExecutor([_fail(ErrorType.NETWORK_ERROR)])
    sched = ToolCallDispatcher(tool_executor=executor, num_workers=1, checkpoint_path=None)
    await sched.start()

    t = QueuedToolCall.new_tool_call("req-1", "always_fail", {})
    fut = t.get_future()
    try:
        await sched.submit(t)
        # 有界等待"已进入第 1 次重试退避"（retry_count 在 sleep 之前自增）
        for _ in range(100):
            if t.retry_count >= 1:
                break
            await asyncio.sleep(0.01)
        assert t.retry_count == 1, "未进入重试退避——测试前提不成立"

        sched.cancel(t.id)
        with pytest.raises(asyncio.CancelledError):
            await fut

        await asyncio.sleep(0.05)
        assert len(executor.call_ids) == 1, "取消后仍发生了下一次调用"
    finally:
        await sched.stop()


@pytest.mark.service
@pytest.mark.asyncio
async def test_shutdown_window_disables_retry_gate(fast_backoff):
    """停机窗口（`_running=False`）里即使错误可重试也**不再重试**。

    这是重试 gate 的第一项（`scheduler.py:624` 的 `self._running`）——黑盒用例碰不到
    这个窗口（gate 在退避之前求值，`stop()` 只能打断退避），所以这里白盒地直接调
    `_execute_tool` 并把 `_running` 置 False，把该子句单独钉住。
    """
    executor = _ScriptedExecutor([_fail(ErrorType.NETWORK_ERROR)])
    sched = ToolCallDispatcher(tool_executor=executor, num_workers=1, checkpoint_path=None)
    await sched.start()
    try:
        sched._running = False  # noqa: SLF001 —— 模拟 shutdown 窗口
        t = QueuedToolCall.new_tool_call("req-1", "always_fail", {})
        result = await sched._execute_tool(t)  # noqa: SLF001 —— 直接驱动重试循环
    finally:
        await sched.stop()

    assert len(executor.call_ids) == 1, "停机期间不得再发起重试"
    assert t.retry_count == 0
    assert not result.success
    assert result.error_type is ErrorType.NETWORK_ERROR


# ═══════════════════════════════════════════════════════════════════
# 四、同一个 tool_call_id 提交两次：没有去重，就是执行两次
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
async def test_same_tool_call_id_submitted_twice_executes_twice():
    """同一个 `tool_call_id` 提交两次（同批 + 跨批）→ **执行两次**，无跨调用去重。

    归因（两处代码，均无"命中即复用"分支）：
    - `dispatch_tool_calls` 对每个 ToolCall 都新建 `QueuedToolCall`
      （`scheduler.py:260-266`），其 `TaskId` 由 `new_task_id()` 现生成
      （`task.py:153`）→ 同名同参调用在调度层不是同一个键；
    - `ToolManager` 里按 `ctx.call_id` 建的 `_tasks` 只是**在途句柄表**
      （`Module/tool/manager.py:255-256` 写入，`finally` 中 pop），没有短路复用——
      它自己的注释就写明了"同一工具多次调用会**互相覆盖 key**"
      （`manager.py:253-254`），即连句柄都不是按 call_id 唯一保存的，更不是结果缓存。
    """
    seen: list[str] = []

    class Scribe(ToolPlugin):
        name = "scribe"
        description = "records its call_id"
        param_model = None
        capabilities = set()
        safety = SafetyProfile()

        async def execute(self, *, ctx: ToolContext, emit=None, params=None) -> ToolResult:
            seen.append(ctx.call_id)
            return ToolResult.ok(name="scribe", content="ok", duration_ms=0.0)

    tm = ToolAdapter([Scribe], session_id="test")
    await tm.start()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=2, checkpoint_path=None)
    await sched.start()
    try:
        # ① 同一批里出现两次同 id 的调用
        same_batch = await sched.dispatch_tool_calls(
            "req-1", [_tc("tc_dup", "scribe", "{}"), _tc("tc_dup", "scribe", "{}")])
        # ② 再跨批提交一次同 id
        next_batch = await sched.dispatch_tool_calls("req-1", [_tc("tc_dup", "scribe", "{}")])
    finally:
        await sched.stop()

    assert len(same_batch) == 2 and len(next_batch) == 1
    assert all(r.success for _c, r in same_batch + next_batch)
    assert len(seen) == 3, "同一 tool_call_id 未被去重——执行了 3 次"
    assert seen == ["tc_dup"] * 3

    # ③ 内部任务 ID 每次都是新的：去重键不可能是 TaskId
    a = QueuedToolCall.new_tool_call("req-1", "scribe", {}, call_id="tc_dup")
    b = QueuedToolCall.new_tool_call("req-1", "scribe", {}, call_id="tc_dup")
    assert a.id != b.id
    assert a.payload["call_id"] == b.payload["call_id"] == "tc_dup"


# ═══════════════════════════════════════════════════════════════════
# 五、调度器合成的超时：**只**在插件声明 idempotent 时才重试
#
#     `_execute_tool` 的 `asyncio.TimeoutError` 分支把超时写成
#     `ErrorType.TIMEOUT`——这个标签是**调度器猜的**，不是插件说的。
#     修复前它和插件自己返回的 TIMEOUT 走同一个 gate（`is_retryable` 为真即重试），
#     于是 write_file / set_reminder / desktop.* 这些**有副作用**的工具在超时后
#     会被用同一个 `call_id` 再 invoke 一次。
#
#     **范围（诚实声明）**：本节的判据只覆盖**调度器这一层**合成的超时。
#     还有第二个合成点——`Module/tool/safety.py::SafetyPipeline.execute` 在
#     `plugin.safety.request_timeout` 处的 wait_for 也合成 TIMEOUT，而它以
#     "插件返回的 ToolResult"形态到达调度器，门控看不见它。算术上它几乎总是
#     先触发（`_timeout_for` = max(60, declared+5) > declared：write_file 30 vs 60、
#     desktop.file.write 120 vs 125），所以**未声明幂等的工具在真实预算下仍会因
#     超时被重试**。该残余由本节末尾的 strict-xfail 用例钉住（等待 owner 决定是否
#     把管道层也纳入门控）。
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
async def test_synthesised_timeout_does_not_retry_when_plugin_declares_not_idempotent(
        monkeypatch, fast_backoff):
    """未声明 `idempotent` 的工具：第 1 次超时 → **恰好调用 1 次**，不再重试。

    This is the regression test for the fix：修复前该场景调用 **2 次**（超时被
    `is_retryable=True` 直接放行），对 write_file / set_reminder / desktop.file.write
    这类工具体现为"第一次已落地的副作用再做一遍"。
    """
    monkeypatch.setattr(_scheduler_mod, "_TOOL_TIMEOUT", 0.05)
    executor = _HangingThenOkExecutor(idempotent=False)
    sched = ToolCallDispatcher(tool_executor=executor, num_workers=1, checkpoint_path=None)
    await sched.start()
    try:
        results = await sched.dispatch_tool_calls(
            "req-1", [_tc("tc_timeout_mutating", "mutating_tool", "{}")])
    finally:
        await sched.stop()

    _call, result = results[0]
    assert executor.attempts == 1, (
        "未声明幂等的工具在调度器合成的超时后**不得**被再次 invoke"
        "（修复前这里是 2——同一个 call_id 再跑一遍副作用）")
    assert executor.call_ids == ["tc_timeout_mutating"]
    assert executor.cancelled_attempts == 1, "第 1 次尝试不是被单次超时取消的"
    assert not result.success
    assert result.error_type is ErrorType.TIMEOUT, (
        "失败原因仍如实上报为 TIMEOUT——被门控的是**重试**，不是错误分类")
    assert result.error_type.is_retryable, (
        "ErrorType.is_retryable 的分类不变（TIMEOUT 仍是可重试分类）")


@pytest.mark.service
@pytest.mark.asyncio
async def test_synthesised_timeout_retries_when_plugin_declares_idempotent(
        monkeypatch, fast_backoff):
    """声明 `idempotent=True` 的工具：超时后**照旧重试**（2 次），`call_id` 不变。

    这条是用例 #1 的反向边界：修复不是"取消所有超时重试"，而是把重试的依据
    从"调度器猜的标签"换成"插件自己的声明"。
    """
    monkeypatch.setattr(_scheduler_mod, "_TOOL_TIMEOUT", 0.05)
    executor = _HangingThenOkExecutor(idempotent=True)
    sched = ToolCallDispatcher(tool_executor=executor, num_workers=1, checkpoint_path=None)
    await sched.start()
    try:
        results = await sched.dispatch_tool_calls(
            "req-1", [_tc("tc_timeout_readonly", "readonly_tool", "{}")])
    finally:
        await sched.stop()

    _call, result = results[0]
    assert result.success, "声明幂等的工具超时后应重试并成功"
    assert executor.attempts == 2, "声明了 idempotent=True 却没有重试——把重试整个关掉了"
    assert executor.cancelled_attempts == 1
    assert executor.call_ids == ["tc_timeout_readonly", "tc_timeout_readonly"]


@pytest.mark.service
@pytest.mark.asyncio
async def test_plugin_declared_timeout_still_retries_even_when_not_idempotent(fast_backoff):
    """插件**自己返回** `ErrorType.TIMEOUT` 是插件的陈述 → 即使 `idempotent=False` 也重试。

    与上一条的区别是**谁说的**：这里是插件在 `ToolResult.fail(error_type=TIMEOUT)`
    里自己写的（如 `SafetyPipeline` 把插件内层超时归一后的结果），不是调度器猜的。
    门控只作用于后者。
    """
    executor = _ScriptedExecutor([_fail(ErrorType.TIMEOUT), _OK], idempotent=False)
    sched = ToolCallDispatcher(tool_executor=executor, num_workers=1, checkpoint_path=None)
    await sched.start()
    try:
        results = await sched.dispatch_tool_calls(
            "req-1", [_tc("tc_plugin_timeout", "plugin_timeout_tool", "{}")])
    finally:
        await sched.stop()

    _call, result = results[0]
    assert result.success
    assert len(executor.call_ids) == 2, (
        "插件自己返回的可重试错误不受 idempotent 影响——修复只针对调度器合成的超时")
    assert executor.call_ids == ["tc_plugin_timeout", "tc_plugin_timeout"]


@pytest.mark.service
@pytest.mark.asyncio
async def test_missing_declares_idempotent_is_loud_and_does_not_retry(monkeypatch, fast_backoff):
    """executor 缺 `declares_idempotent` → **AttributeError 上抛**（不 getattr 兜底）。

    与 `_timeout_for` 的纪律一致：缺方法时**不得**静默退回"就当声明了 True"。
    兜底会把这个洞藏起来，而且方向还是危险的那一侧（重复副作用）。结果由
    `_outcome_of` 归一为不可重试的 `TOOL_ERROR`——于是既没有重试，也没有把
    "接口缺实现"伪装成 TIMEOUT。
    """
    monkeypatch.setattr(_scheduler_mod, "_TOOL_TIMEOUT", 0.05)

    class _NoDeclaration:
        """只实现 `declared_request_timeout` + `execute`（缺新方法）。"""

        def __init__(self) -> None:
            self.attempts = 0

        def declared_request_timeout(self, tool_name: str) -> float | None:
            return None

        async def execute(self, tool_name, *, ctx, **kwargs):
            self.attempts += 1
            try:
                await asyncio.wait_for(asyncio.Event().wait(), timeout=5.0)
            except asyncio.CancelledError:
                raise
            raise AssertionError("第 1 次尝试未被单次超时取消")

    executor = _NoDeclaration()
    sched = ToolCallDispatcher(tool_executor=executor, num_workers=1, checkpoint_path=None)
    await sched.start()
    try:
        results = await sched.dispatch_tool_calls(
            "req-1", [_tc("tc_missing", "hanging_tool", "{}")])
    finally:
        await sched.stop()

    _call, result = results[0]
    assert executor.attempts == 1
    assert not result.success
    assert result.error_type is ErrorType.TOOL_ERROR
    assert "declares_idempotent" in (result.error or ""), (
        "缺失应当**响亮**地出现在错误里，而不是被静默吞掉")


@pytest.mark.service
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "idempotent,expected_attempts,expect_success",
    [(True, 2, True), (False, 1, False)],
    ids=["declared_idempotent", "not_declared"],
)
async def test_synthesised_timeout_gate_through_real_pipeline(
        monkeypatch, fast_backoff, idempotent, expected_attempts, expect_success):
    """真实 `ToolAdapter` 委托链上的同一条边界（插件声明 → manager → adapter → 调度器）。

    修复前两种参数下都是 2 次；修复后 `idempotent=False` 只跑 1 次。
    """
    monkeypatch.setattr(_scheduler_mod, "_TOOL_TIMEOUT", 0.05)
    seen: list[int] = []
    cancelled: list[int] = []
    plugin_cls = _hanging_plugin_class(
        idempotent=idempotent, seen=seen, cancelled=cancelled)
    tm = ToolAdapter([plugin_cls], session_id="test")
    await tm.start()

    sched = ToolCallDispatcher(
        tool_executor=_ShrunkBudgetAdapter(tm), num_workers=1, checkpoint_path=None)
    await sched.start()
    try:
        results = await sched.dispatch_tool_calls(
            "req-1", [_tc("tc_real_timeout", "hanging_real", "{}")])
    finally:
        await sched.stop()

    _call, result = results[0]
    assert len(seen) == expected_attempts, (
        f"idempotent={idempotent} 时应调用 {expected_attempts} 次")
    assert result.success is expect_success
    assert cancelled == [1], "第 1 次尝试必须是被单次超时取消的（否则用例前提不成立）"
    if not expect_success:
        assert result.error_type is ErrorType.TIMEOUT


@pytest.mark.service
@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "**范围外残余**（2026-09-16）：本次修复只门控『调度器自己合成』的超时。"
        "`SafetyPipeline.execute` 在 `plugin.safety.request_timeout` 处的 wait_for "
        "**也**合成 TIMEOUT，而它以『插件返回的 ToolResult』形态到达调度器——"
        "调度层无法与插件自己写的 TIMEOUT 区分（这正是『谁说的』判据的边界）。"
        "而且它几乎总是先触发：`_timeout_for` = max(60, declared+5) > declared"
        "（实测 write_file 30 vs 60、desktop.file.write 120 vs 125），"
        "所以真实预算下『未声明幂等的工具因超时被重试』仍然会发生。"
        "本用例断言的是更宽的原则（未声明幂等的工具超时后只应被调用一次）；"
        "若 owner 决定把管道层也纳入 `idempotent` 门控（例如让管道合成的 TIMEOUT "
        "按 idempotent 降级为不可重试），本用例会 XPASS（strict → 红）→ 届时改为正例。"
    ),
)
async def test_pipeline_synthesised_timeout_is_not_gated_yet(fast_backoff):
    """已知残余：**管道层**合成的超时不受 `idempotent` 门控（预期 XFAIL）。

    插件声明 `request_timeout=0.05`（模拟"内层预算先于调度器 60s 触发"）后挂起：
    先触发的是 `SafetyPipeline` 的 wait_for，它返回 `ToolResult.fail(TIMEOUT)`；
    调度器把这条当"插件自己返回的可重试错误"，于是照旧重试
    （3 次 = 首发 + 默认 max_retries=2，`fast_backoff` 只影响等待时长）。
    """
    seen: list[int] = []

    class _PipelineTimeout(ToolPlugin):
        name = "pipeline_timeout_tool"
        description = "side-effecting tool that hangs under a tiny declared budget"
        param_model = None
        capabilities = set()
        safety = SafetyProfile(request_timeout=0.05)  # idempotent 缺省 False

        async def execute(self, *, ctx: ToolContext, emit=None, params=None) -> ToolResult:
            seen.append(1)
            await asyncio.sleep(5)
            return ToolResult.ok(name=self.name, content="ok", duration_ms=0.0)

    tm = ToolAdapter([_PipelineTimeout], session_id="test")
    await tm.start()
    sched = ToolCallDispatcher(tool_executor=tm, num_workers=1, checkpoint_path=None)
    await sched.start()
    try:
        results = await sched.dispatch_tool_calls(
            "req-1", [_tc("tc_pipe_timeout", "pipeline_timeout_tool", "{}")])
    finally:
        await sched.stop()

    _call, result = results[0]
    assert not result.success
    assert result.error_type is ErrorType.TIMEOUT
    assert len(seen) == 1, "未声明幂等的工具不应因管道层合成的超时被重复调用"


# ═══════════════════════════════════════════════════════════════════
# 六、可重试性的事实源：ErrorType 管"插件说的"，idempotent 管"调度器猜的"
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_retry_semantics_are_split_between_error_type_and_idempotent():
    """声明面契约（2026-09-16 起）：`ErrorType.is_retryable` + `SafetyProfile.idempotent`。

    - `SafetyProfile.idempotent` **存在**且默认 `False`（fail-safe：忘记声明 → 少重试）；
    - `ToolPlugin` 上仍然没有 `idempotent`/`retryable` **类属性**（声明只在
      `SafetyProfile` 上，避免"插件级字段"与"安全声明"两个真相源）；
    - `ErrorType.is_retryable` 的可重试集合不变
      （TIMEOUT / RATE_LIMITED / NETWORK_ERROR，见 `Module/_common/error_type.py`）。

    这条用例是原 `test_retryability_is_not_declarable_by_plugin` 的**契约更新**
    （它自己写明："若有人给 SafetyProfile 加 idempotent 字段，本用例会红——那时应
    同步更新本文件的重试边界契约"）：现在字段加上了，契约也一并改写为上面两条。
    """
    profile_fields = {f.name for f in fields(SafetyProfile)}
    assert "idempotent" in profile_fields, "SafetyProfile 必须能声明幂等"
    assert SafetyProfile().idempotent is False, (
        "默认必须 False——fail-safe 方向是'少重试一次'，不是'把副作用做两遍'")
    for forbidden in ("idempotent", "idempotency", "retryable"):
        assert forbidden not in set(dir(ToolPlugin)), (
            f"ToolPlugin 上出现了 {forbidden!r} 类属性——声明应只在 SafetyProfile 上")

    assert set(_RETRYABLE_TYPES) == {
        ErrorType.TIMEOUT, ErrorType.RATE_LIMITED, ErrorType.NETWORK_ERROR}
    assert set(_NON_RETRYABLE_TYPES) == {
        ErrorType.INVALID_ARGUMENT, ErrorType.PERMISSION_DENIED, ErrorType.UNAVAILABLE,
        ErrorType.CANCELLED, ErrorType.TOOL_ERROR, ErrorType.UNKNOWN_TOOL,
        ErrorType.GATE_REJECTED}
    # 副作用类工具的失败路径用的都是不可重试分类（filesystem 写 / reminder / desktop）：
    # 于是"重试重复副作用"只剩超时这一条入口（见 §五的 idempotent 门控）。
    assert ErrorType.TOOL_ERROR not in _RETRYABLE_TYPES
    assert ErrorType.PERMISSION_DENIED not in _RETRYABLE_TYPES


# ═══════════════════════════════════════════════════════════════════
# 七、已知缺口：按 callId 的幂等（目标 spec 承诺 vs 当前实现）
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.service
@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "spec（docs/archive/15-Cyrene-后端改造执行上下文与Prompt-v0.1.md "
        "§四.13「所有副作用工具都按 callId 幂等」/ §批次 4「相同 callId 不重复产生副作用」）"
        "未被调度层实现：重试用同一个 ctx.call_id 再 invoke（scheduler.py:586,596），"
        "调度层无任何按 call_id 的去重存储；Desktop 桥的去重键是 "
        "(tool_name, arguments_hash) 且超时/offline 后即释放（core/desktop/bridge.py:202-252）。"
        "2026-09-16 的 `SafetyProfile.idempotent` 门控只收窄了『**调度器自己合成**的"
        "超时』这一入口；本用例走的是插件**自己返回**的 NETWORK_ERROR（插件的陈述），"
        "按设计仍然重试——所以本用例预期仍 XFAIL（缺口是 callId 级去重，不是超时重试）。"
        "本用例断言的是 spec 的承诺；实现补齐后它会 XPASS（strict → 红），"
        "届时把断言改为正例并更新契约文档。"
    ),
)
async def test_callid_side_effect_uniqueness_promise_not_met(fast_backoff):
    """同一 `call_id` 只应产生一次副作用（spec 承诺）——当前实现**不提供**该保证。

    `strict=True`：实现补齐后本用例变 XPASS → 红，提醒把缺口从报告里划掉。
    退避中性化只影响等待时长，不影响本用例的判据。
    """
    executor = _ScriptedExecutor([_fail(ErrorType.NETWORK_ERROR), _OK])
    sched = ToolCallDispatcher(tool_executor=executor, num_workers=1, checkpoint_path=None)
    await sched.start()
    try:
        await sched.dispatch_tool_calls("req-1", [_tc("tc_once", "side_effect_tool", "{}")])
    finally:
        await sched.stop()

    assert len(executor.side_effects) == 1, "同一 callId 产生了多次副作用"
