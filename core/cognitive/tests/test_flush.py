# 测试文件：core/cognitive/tests/test_flush.py
"""flush_after_response 单元测试 — shutdown 时内存持久化关键路径。

#20b 之后本方法走 `AsyncTaskScope.drain`（**总预算**语义），不再是"逐任务 wait_for +
外层总超时"。用例因此改为**经作用域 spawn/adopt** 造任务——这正是新契约：作用域是
after-response 收尾任务的唯一所有权与唯一收尾点。
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.cognitive.loop import CognitiveLoop

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════

def _make_loop() -> CognitiveLoop:
    """创建一个最小 mock 的 CognitiveLoop 实例，仅用于测试 flush。"""
    return CognitiveLoop(
        llm=MagicMock(),
        fsm=MagicMock(),
        event_bus=MagicMock(),
        output=MagicMock(),
        prompt=MagicMock(),
        session_resolver=AsyncMock(),
    )


def _spawn(loop: CognitiveLoop, coro) -> asyncio.Task:
    """把任务登记进 loop 的 after-response 作用域（唯一所有权）。"""
    return loop._after_response_scope.spawn(coro)  # noqa: SLF001


# ═══════════════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_flush_empty_returns_immediately():
    """无任务 → 直接返回。"""
    loop = _make_loop()
    await loop.flush_after_response()
    # 不应抛异常，不应挂起


@pytest.mark.asyncio
async def test_flush_all_done_returns_immediately():
    """全部已完成 → 直接返回（已完成的任务不在 pending 视图里）。"""
    loop = _make_loop()
    task = _spawn(loop, asyncio.sleep(0))
    await task  # 等它完成
    assert loop._pending_after_response == []  # noqa: SLF001
    await loop.flush_after_response()


@pytest.mark.asyncio
async def test_flush_pending_completes():
    """等待 pending task 正常完成。"""
    loop = _make_loop()
    task = _spawn(loop, asyncio.sleep(0))
    await loop.flush_after_response()
    assert task.done()
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_flush_timeout_cancels_task():
    """超时 → 取消未落定的任务（drain 的收尾语义）。"""
    loop = _make_loop()
    task = _spawn(loop, asyncio.sleep(999))

    await loop.flush_after_response(timeout=0.01)

    assert task.cancelled()


@pytest.mark.asyncio
async def test_flush_exception_does_not_propagate(caplog):
    """task 内部异常 → 不上抛（异常由作用域的 done 回调上报，不再靠 flush 逐任务 catch）。"""
    loop = _make_loop()

    async def raise_err():
        raise RuntimeError("模拟后处理异常")

    task = _spawn(loop, raise_err())

    with caplog.at_level(logging.ERROR):
        await loop.flush_after_response()

    assert task.done()
    assert "后台任务未处理异常" in caplog.text, (
        "异常必须有人上报（作用域的 done 回调），否则只剩 asyncio 的 GC 噪音")


@pytest.mark.asyncio
async def test_flush_budget_is_total_not_per_task():
    """#20b 的核心语义：预算是**总预算**——3 个各睡 10s 的任务不该花 3 × 预算。

    旧实现逐任务 `wait_for(t, 5.0)`，N 个任务能把关闭时间拖成 N × 5s；
    新实现共享一个 deadline。这里用"总耗时远小于 任务数 × 预算"来钉住差异。
    """
    loop = _make_loop()
    for _ in range(3):
        _spawn(loop, asyncio.sleep(10))     # 三个都不会自然完成

    start = asyncio.get_running_loop().time()
    await loop.flush_after_response(timeout=0.2)
    elapsed = asyncio.get_running_loop().time() - start

    assert elapsed < 0.6, (
        f"耗时 {elapsed:.2f}s ≈ 3 × 0.2s —— 退回了逐任务预算语义")


@pytest.mark.asyncio
async def test_restart_reset_cancels_leftovers():
    """重启复位：未落定的任务要**被取消**，而不是只丢掉引用（丢引用=无人观察的孤儿）。"""
    loop = _make_loop()
    task = _spawn(loop, asyncio.sleep(999))

    loop._cancel_after_response()  # noqa: SLF001
    await asyncio.sleep(0)

    assert task.cancelled()


@pytest.mark.asyncio
async def test_adopted_external_task_is_drained():
    """#20b：外部创建的任务（性格反思 tail）经 `adopt` 纳入**同一**收尾域。

    这条钉住"同一个作用域只有一条收尾路径"：drain 必须看得见 adopt 进来的任务，
    否则它又变成那张另立的列表。
    """
    loop = _make_loop()
    external = asyncio.create_task(asyncio.sleep(0))    # 创建点在别处
    loop._after_response_scope.adopt(external)          # noqa: SLF001

    await loop.flush_after_response()

    assert external.done()


@pytest.mark.asyncio
async def test_task_registered_during_flush_is_not_dropped():
    """S3-R2-1：flush **期间**新登记的任务不得被丢弃。

    旧实现是"快照 → 逐个 wait → 无条件 `clear()`"：clear 会把 flush 期间（正 await 的
    tail 内）新登记的任务一并摘除，此后无人 await、也无人观察。
    `#20b` 把收尾域换成 `AsyncTaskScope` 后**没有 clear 这一步**——后来者仍被强引用、
    仍被观察，且下一次 drain 会等它。本用例钉住这个语义（它正是 S3-R2-1 剩下的那一半）。
    """
    loop = _make_loop()
    late: list[asyncio.Task] = []

    async def _late_work():
        await asyncio.sleep(0.05)

    async def _first_work():
        await asyncio.sleep(0.01)
        late.append(_spawn(loop, _late_work()))   # flush 正在等第一个任务时登记第二个
        await asyncio.sleep(0)

    _spawn(loop, _first_work())
    await loop.flush_after_response()             # 这一次只覆盖第一个任务（快照语义）
    assert late, "前置条件：后来者已登记"

    scope = loop._after_response_scope  # noqa: SLF001
    assert late[0] in scope.pending() or late[0].done(), (
        "flush 期间新登记的任务被丢弃了（旧实现的 clear() 行为）")

    await loop.flush_after_response()             # 再 drain 一次必须覆盖它
    assert late[0].done() and not late[0].cancelled(), "后来者没能正常落定"
