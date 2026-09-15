# 测试文件：core/agent/tests/test_shutdown_steps_matrix.py
"""`Agent.stop()` 真实关停表 × 失败类别的全矩阵 —— 逐步注入失败，验证序列不被截断。

**为什么需要本文件**（CODE_REVIEW_REPORT.md #46 gap 行 #3）：`stop()` 的**通用机制**
已有测试覆盖，但覆盖是"隔着合成表"的——
`test_shutdown_resilience.py` 用自造的 2–3 步表（`[("a","_step_a"), ("b","_step_b")]`）
证明"一步炸不截断其余"，`test_lifecycle_steps.py` 只验结构（名字唯一、方法存在、
flush 与 stop 的相对顺序）。**没有任何测试把失败注入到真实的那张表里**：某个真实
步骤（如 `_flush_consolidator`、`_stop_mailbox`）一旦把自己的失败升级成"中断整个
循环"，现有测试全绿，而代价是会话落库 / 归档 drain / 生成终态整段不做。

本文件把 `AgentLifecycle._SHUTDOWN_STEPS` 的**每个真实条目**逐一参数化，只让该步
失败，然后断言：其余每一步都照跑、循环之后的收尾照走、异常语义严格按 CR-02 契约。

**契约（全部取自 `lifecycle.py::stop()` 的实现与既有韧性测试，未发明语义）**：
1. 逐步 `except BaseException` → **任何**一步失败都不截断，循环继续跑完剩余步骤；
2. 普通 `Exception` 被吞掉，只记 warning，`stop()` 正常返回；
3. `CancelledError` / `KeyboardInterrupt`（BaseException 但非 Exception）同样不截断，
   但要在**收尾之后**重抛（不吞取消/中断语义）；
4. 收尾 = `_lifecycle_tasks.drain(timeout=2.0)` + `_session_manager.clear_work()`，
   它们在重抛**之前**执行；
5. `_state` 在 `stop()` 入口即置 STOPPED，任何步骤失败都不会把它改回去。
"""

import asyncio
import inspect
import logging

import pytest

from core.agent._types import RunState
from core.agent.agent import Agent
from core.agent.lifecycle import AgentLifecycle

pytestmark = pytest.mark.service

# 参数集**直接派生自真实类属性**（不是手抄一份字面量）：新增第 N 个关停步骤 →
# 矩阵自动多 N 个 case。派生关系本身由
# `test_parametrization_is_derived_from_real_shutdown_table` 守卫——参数集若被改成
# 硬编码字面量或空列表，那条用例会红（空参数集在 pytest 里只会变成 skip，全文件
# 依旧"全绿"，正是本文件最容易假绿的地方）。
_SHUTDOWN_STEPS: list[tuple[str, str]] = list(AgentLifecycle._SHUTDOWN_STEPS)
_STEP_IDS: list[str] = [name for name, _ in _SHUTDOWN_STEPS]

# 失败类别：ordinary = Exception（吞掉继续）；critical = BaseException 非 Exception
# （跑完收尾后重抛）。分类来自 `stop()` 里的 `isinstance(exc, Exception)` 判据。
_ORDINARY = "ordinary"
_CRITICAL = "critical"
_FAILURE_CASES: list[tuple[type[BaseException], str]] = [
    (RuntimeError, _ORDINARY),
    (asyncio.CancelledError, _CRITICAL),
    (KeyboardInterrupt, _CRITICAL),
]
_FAILURE_IDS: list[str] = ["RuntimeError", "CancelledError", "KeyboardInterrupt"]


class _StubLifecycleScope:
    """`AsyncTaskScope` 替身——记录 `stop()` 收尾的兜底 drain。

    真身 `drain` 是"总预算"语义（`timeout` 为关键字参数），签名必须一致，
    否则 `stop()` 的收尾一 await 就 TypeError。
    """

    def __init__(self) -> None:
        self.drain_timeouts: list[float | None] = []

    async def drain(self, *, timeout: float | None = None) -> int:
        self.drain_timeouts.append(timeout)
        return 0


class _StubSessionManager:
    """`SessionManager` 替身——记录 `stop()` 收尾的 `clear_work()`。"""

    def __init__(self) -> None:
        self.clear_work_calls = 0

    def clear_work(self) -> None:
        self.clear_work_calls += 1


def _make_step_stub(
    *,
    step_name: str,
    failure: BaseException | type[BaseException] | None,
    ran: list[str],
    is_async: bool,
):
    """造一个步骤桩：先记录自己跑过，再（可选地）抛指定失败。

    桩的同步/异步性**与真实方法一致**（由调用方用 `inspect.iscoroutinefunction` 判定）
    ——真实表里 `_stop_cognitive_and_tts` 是同步方法，`stop()` 的
    `hasattr(result, "__await__")` 两个分支都应当被走到，桩不建模这一点就等于
    把其中一条分支从矩阵里悄悄删掉。
    """
    if is_async:
        async def _async_stub() -> None:
            ran.append(step_name)
            if failure is not None:
                raise failure
        return _async_stub

    def _sync_stub() -> None:
        ran.append(step_name)
        if failure is not None:
            raise failure
    return _sync_stub


def _build_stub_agent(
    ran: list[str],
    *,
    failures: dict[str, BaseException | type[BaseException]] | None = None,
) -> Agent:
    """最小 Agent 替身：**真实** `stop()` + **真实** `_SHUTDOWN_STEPS`，步骤全换桩。

    `object.__new__(Agent)` + 只设 `stop()` 真正会碰的字段（沿用
    `test_shutdown_mailbox_delivery.py::_make_stub_agent` 的桩法，不建真 Agent，
    不连库、不 spawn 后台任务）。实例属性遮蔽同名方法 → `getattr(self, method_name)`
    拿到的是桩；`_SHUTDOWN_STEPS` 保持类属性（本文件不覆盖它，覆盖了就测不到真实表）。
    """
    failures = failures or {}
    agent = object.__new__(Agent)
    agent._state = RunState.RUNNING          # 与 Agent.__init__ 的初始语义一致  # noqa: SLF001
    agent._pending_messages = []                                                  # noqa: SLF001
    agent._lifecycle_tasks = _StubLifecycleScope()                                # noqa: SLF001
    agent._session_manager = _StubSessionManager()                                # noqa: SLF001

    for name, method_name in _SHUTDOWN_STEPS:
        real = getattr(AgentLifecycle, method_name)
        setattr(agent, method_name, _make_step_stub(
            step_name=name,
            failure=failures.get(method_name),
            ran=ran,
            is_async=inspect.iscoroutinefunction(real),
        ))
    return agent


def _all_step_names() -> list[str]:
    """真实关停表的步骤名（每次现算，防止与参数集之外的副本漂移）。"""
    return [name for name, _ in AgentLifecycle._SHUTDOWN_STEPS]


# ═══════════════════════════════════════════════════════════════════
# 1. 假绿守卫：参数集确实派生自真实表、且非空
# ═══════════════════════════════════════════════════════════════════

def test_parametrization_is_derived_from_real_shutdown_table():
    """参数集必须非空、逐项等于真实表、无重复——否则矩阵会整体 skip 成假绿。

    pytest 对空参数集的处理是**把用例标成 skip**（"got empty parameter set"），
    退出码仍是 0。所以"矩阵全绿"本身不能证明跑过任何一步，必须由本用例守门。
    """
    real = list(AgentLifecycle._SHUTDOWN_STEPS)
    assert real, "_SHUTDOWN_STEPS 不得为空（空表 → 矩阵用例整体 skip）"
    assert len(real) >= 2, "至少两步，否则'失败不截断'无从谈起"
    assert _SHUTDOWN_STEPS == real, "参数集必须是真实表的副本（含顺序）"
    assert _STEP_IDS == [name for name, _ in real]
    assert len(set(_STEP_IDS)) == len(_STEP_IDS), "步骤名不得重复"
    assert len({m for _, m in real}) == len(real), "方法名不得重复"
    # 失败类别必须同时覆盖"吞掉"与"重抛"两侧，否则矩阵缺一半语义
    assert len(_FAILURE_CASES) >= 3
    assert {kind for _, kind in _FAILURE_CASES} == {_ORDINARY, _CRITICAL}
    # 分类判据（`isinstance(exc, Exception)`）依赖 Python 的异常层级，现算不写死
    for exc_type, kind in _FAILURE_CASES:
        assert issubclass(exc_type, BaseException)
        expected = _ORDINARY if issubclass(exc_type, Exception) else _CRITICAL
        assert kind == expected, f"{exc_type.__name__} 的失败类别与异常层级不符"


# ═══════════════════════════════════════════════════════════════════
# 2. 全矩阵：每个真实步骤 × 每个失败类别
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("failure,kind", _FAILURE_CASES, ids=_FAILURE_IDS)
@pytest.mark.parametrize("step_name,method_name", _SHUTDOWN_STEPS, ids=_STEP_IDS)
async def test_every_real_step_failure_does_not_truncate_shutdown(
    step_name, method_name, failure, kind, caplog,
):
    """**只让** `step_name` 失败：其余每一步照跑，收尾照走，异常语义按契约。

    - RuntimeError（ordinary）：`stop()` 正常返回，失败步骤记 warning；
    - CancelledError / KeyboardInterrupt（critical）：同样跑完全序列 + 收尾，
      然后重抛该异常（`pytest.raises` 之外仍断言收尾计数，故"重抛发生在收尾之后"
      是被真的钉死的——早抛的话 drain/clear_work 计数会是 0）。
    """
    assert (step_name, method_name) in AgentLifecycle._SHUTDOWN_STEPS, (
        f"参数 {step_name!r}/{method_name!r} 不在真实关停表里——参数集与真实表漂移"
    )
    ran: list[str] = []
    agent = _build_stub_agent(ran, failures={method_name: failure})

    with caplog.at_level(logging.WARNING, logger="core.agent.lifecycle"):
        if kind == _ORDINARY:
            await agent.stop()               # 普通异常被吞：不得抛出
        else:
            with pytest.raises(failure):
                await agent.stop()

    # ① 不截断：全序列逐步跑完，且顺序与真实表完全一致（失败步自己也标记为跑过）
    assert ran == _all_step_names(), (
        f"{step_name} 抛 {failure.__name__} 后关停序列被截断/乱序：{ran}"
    )
    # ② 显式列出"其余每一步都在"——失败信息比整表比对更好读
    others = [name for name in _all_step_names() if name != step_name]
    assert set(others) <= set(ran), (
        f"{step_name} 失败后有步骤没跑：{sorted(set(others) - set(ran))}"
    )

    # ③ 循环之后的收尾也走到了（critical 亦如此 → 重抛在收尾之后，见 docstring）
    assert agent._lifecycle_tasks.drain_timeouts == [2.0], (      # noqa: SLF001
        "stop() 收尾的兜底 drain 必须执行（且用 2.0s 预算）"
    )
    assert agent._session_manager.clear_work_calls == 1, (         # noqa: SLF001
        "stop() 收尾的 clear_work 必须执行"
    )

    # ④ 状态：入 stop() 即 STOPPED，失败不把它改回去
    assert agent._state == RunState.STOPPED                        # noqa: SLF001

    # ⑤ 失败必须留日志（warning 级，含步骤名）——不静默吞
    records = [r for r in caplog.records
               if r.getMessage() == f"Shutdown step {step_name} failed"]
    assert records, f"{step_name} 的失败没有留下 'Shutdown step ... failed' 日志"
    assert all(r.levelno >= logging.WARNING for r in records)


@pytest.mark.asyncio
async def test_matrix_control_all_steps_succeed(caplog):
    """对照组：全部步骤正常 → 全序列跑完、无失败日志、收尾恰好一次。

    没有这一条，"所有矩阵用例都因为别的原因提前失败"这种整体性坏掉会被误读成
    "失败注入生效了"。
    """
    ran: list[str] = []
    agent = _build_stub_agent(ran)

    with caplog.at_level(logging.WARNING, logger="core.agent.lifecycle"):
        await agent.stop()

    assert ran == _all_step_names()
    assert agent._lifecycle_tasks.drain_timeouts == [2.0]      # noqa: SLF001
    assert agent._session_manager.clear_work_calls == 1        # noqa: SLF001
    assert agent._state == RunState.STOPPED                    # noqa: SLF001
    assert not [r for r in caplog.records
                if "Shutdown step" in r.getMessage() and r.levelno >= logging.WARNING]

    # 桩必须逐项镜像真实方法的同步/异步性——否则矩阵覆盖不到 stop() 的同步分支
    for _name, method_name in _SHUTDOWN_STEPS:
        assert (inspect.iscoroutinefunction(getattr(agent, method_name))
                == inspect.iscoroutinefunction(getattr(AgentLifecycle, method_name))), (
            f"{method_name} 的桩与真实方法同步性不一致"
        )


@pytest.mark.asyncio
async def test_only_first_critical_failure_is_reraised():
    """多个 critical 失败 → 只重抛**第一个**（后续同样只记日志）。

    取自实现 `if not isinstance(exc, Exception) and pending_exc is None`：重抛第一个
    才不掩盖最早的中断根因；若实现改成"重抛最后一个"，本用例红。
    """
    first_method = _SHUTDOWN_STEPS[0][1]
    second_method = _SHUTDOWN_STEPS[1][1]
    first = KeyboardInterrupt("第一个中断")
    second = asyncio.CancelledError()
    ran: list[str] = []
    agent = _build_stub_agent(ran, failures={first_method: first, second_method: second})

    with pytest.raises(KeyboardInterrupt) as excinfo:
        await agent.stop()

    assert excinfo.value is first, "必须重抛第一个 critical 异常（实例同一性），不掩盖根因"
    assert ran == _all_step_names(), "两次 critical 失败同样不得截断序列"
    assert agent._lifecycle_tasks.drain_timeouts == [2.0]      # noqa: SLF001
    assert agent._session_manager.clear_work_calls == 1        # noqa: SLF001
