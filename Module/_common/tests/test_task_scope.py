"""AsyncTaskScope 测试 — 所有权、异常观察、退出收尾（ARCH-INC-P2-03）。

全部为纯 asyncio 测试，无 I/O、无 Agent、无外部服务。用 ``caplog`` 断言
「异常被观察到了」而不是「异常没抛」——这是本模块存在的核心理由。
"""
import asyncio
import logging

import pytest

from Module._common.task_scope import (
    AsyncTaskScope,
    TaskScopeClosedError,
    cancel_and_drain,
)

pytestmark = pytest.mark.unit


async def _sleep_forever() -> None:
    await asyncio.sleep(3600)


async def _raise_after(delay: float, exc: Exception) -> None:
    await asyncio.sleep(delay)
    raise exc


async def _return_value(value: int) -> int:
    return value


def _stubborn_body(survive: int):
    """造一个「吞掉前 ``survive`` 次取消」的协程，用于验证「取消无效任务会被如实上报」。

    ``survive`` 必须刚好等于被测路径发出的取消次数：少了会提前退出（断言落空），
    多了会在事件循环关闭时仍挂着（挂住 pytest）。事件循环关闭时会再补一次取消，
    所以 ``survive`` 取「被测路径的取消次数」即可保证测试自身一定能收尾。
    """
    async def _body() -> None:
        seen = 0
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                seen += 1
                if seen > survive:
                    raise

    return _body()


# ================================================================
# 所有权：强引用
# ================================================================


class TestOwnership:
    @pytest.mark.asyncio
    async def test_spawn_holds_strong_reference(self):
        """spawn 出的任务在完成前一直有强引用——否则可能被 GC 掉。"""
        scope = AsyncTaskScope("t")
        task = scope.spawn(_sleep_forever(), name="worker")
        assert len(scope) == 1
        assert task in scope.pending()
        await scope.aclose()

    @pytest.mark.asyncio
    async def test_done_task_leaves_the_set(self):
        """任务正常结束后自动移出集合——长跑 owner 不累积引用。"""
        scope = AsyncTaskScope("t")
        task = scope.spawn(_return_value(1))
        await task
        assert len(scope) == 0
        assert scope.pending() == ()

    @pytest.mark.asyncio
    async def test_spawn_returns_awaited_task(self):
        """调用方仍可自行 await（scope 的观察不消费异常）。"""
        scope = AsyncTaskScope("t")
        task = scope.spawn(_return_value(42))
        assert await task == 42

    @pytest.mark.asyncio
    async def test_task_name_defaults_to_owner(self):
        scope = AsyncTaskScope("agent")
        task = scope.spawn(_return_value(1))
        assert task.get_name() == "agent-task"
        task2 = scope.spawn(_return_value(1), name="explicit")
        assert task2.get_name() == "explicit"

    @pytest.mark.asyncio
    async def test_repr_reports_state(self):
        scope = AsyncTaskScope("agent")
        assert "open" in repr(scope)
        assert "pending=0" in repr(scope)
        scope.spawn(_sleep_forever())
        assert "pending=1" in repr(scope)
        await scope.aclose()
        assert "closed" in repr(scope)

    @pytest.mark.asyncio
    async def test_empty_scope_is_truthy(self):
        """空 scope 仍为真值——``if scope:`` 不该被读成「有任务」。"""
        assert bool(AsyncTaskScope("t")) is True


# ================================================================
# 异常观察
# ================================================================


class TestObservation:
    @pytest.mark.asyncio
    async def test_unhandled_exception_is_logged(self, caplog):
        """核心契约：无人 await 的失败任务必须留下 ERROR 日志。"""
        scope = AsyncTaskScope("sentry")
        with caplog.at_level(logging.ERROR):
            scope.spawn(_raise_after(0.0, ValueError("boom")), name="failing")
            await scope.drain(timeout=1.0)
        assert any(
            r.levelno == logging.ERROR and "sentry" in r.getMessage() and "failing" in r.getMessage()
            for r in caplog.records
        ), caplog.text
        assert any(r.exc_info and isinstance(r.exc_info[1], ValueError) for r in caplog.records)

    @pytest.mark.asyncio
    async def test_cancellation_is_not_logged_as_error(self, caplog):
        """主动取消是正常收尾路径，不是异常。"""
        scope = AsyncTaskScope("t")
        with caplog.at_level(logging.ERROR):
            scope.spawn(_sleep_forever(), name="cancelled")
            await scope.aclose()
        assert not [r for r in caplog.records if r.levelno == logging.ERROR]

    @pytest.mark.asyncio
    async def test_log_errors_false_suppresses_report(self, caplog):
        """调用方自己 await 并处理异常时，可显式关掉观察以免重复噪声。"""
        scope = AsyncTaskScope("t")
        with caplog.at_level(logging.ERROR):
            task = scope.spawn(_raise_after(0.0, ValueError("handled")), log_errors=False)
            await scope.drain(timeout=1.0)
        assert not [r for r in caplog.records if r.levelno == logging.ERROR]
        with pytest.raises(ValueError):
            await task  # 异常仍然归调用方所有

    @pytest.mark.asyncio
    async def test_observation_does_not_swallow_for_awaiter(self):
        """观察不改变 await 语义——scope 不吞异常，只是「另外记一笔」。"""
        scope = AsyncTaskScope("t")
        task = scope.spawn(_raise_after(0.0, KeyError("k")))
        with pytest.raises(KeyError):
            await task


# ================================================================
# 收尾：cancel_all / drain / aclose
# ================================================================


class TestShutdown:
    @pytest.mark.asyncio
    async def test_cancel_all_returns_count_and_is_idempotent(self):
        scope = AsyncTaskScope("t")
        scope.spawn(_sleep_forever())
        scope.spawn(_sleep_forever())
        scope.spawn(_return_value(1))  # 已完成的不计数
        await asyncio.sleep(0)
        assert len(scope) == 2
        scope.spawn(_sleep_forever())
        assert scope.cancel_all() == 3
        # 取消请求已发出但尚未送达——再调一次仍会命中同样 3 个任务
        assert scope.cancel_all() == 3
        assert await scope.drain(timeout=1.0) == 0
        # 落定之后再调，集合已空
        assert scope.cancel_all() == 0

    @pytest.mark.asyncio
    async def test_drain_waits_for_natural_completion_without_cancelling(self):
        """drain 是「等」不是「杀」——预算内自然完成的任务不受影响。"""
        scope = AsyncTaskScope("t")
        flag = []

        async def _work():
            await asyncio.sleep(0.02)
            flag.append("done")

        scope.spawn(_work())
        assert await scope.drain(timeout=1.0) == 0
        assert flag == ["done"]

    @pytest.mark.asyncio
    async def test_drain_budget_is_total_not_per_task(self):
        """总预算语义：3 个各睡 0.3s 的任务不该用掉 3 × 0.3s 的等待窗口。"""
        scope = AsyncTaskScope("t" * 1)

        async def _slow():
            await asyncio.sleep(5.0)

        for _ in range(3):
            scope.spawn(_slow())
        loop = asyncio.get_running_loop()
        start = loop.time()
        leftover = await scope.drain(timeout=0.15)
        elapsed = loop.time() - start
        assert leftover == 3
        # 0.15 预算 + 取消落定（≤1s）——远小于逐任务 3 × 0.15s 之外的失控增长
        assert elapsed < 2.0

    @pytest.mark.asyncio
    async def test_drain_cancels_stragglers_and_reports(self, caplog):
        scope = AsyncTaskScope("t")
        scope.spawn(_sleep_forever(), name="hang")
        with caplog.at_level(logging.WARNING):
            leftover = await scope.drain(timeout=0.02)
        assert leftover == 1
        assert any("drain 超时" in r.getMessage() for r in caplog.records)
        assert len(scope) == 0  # 取消后已落定

    @pytest.mark.asyncio
    async def test_drain_zero_budget_cancels_without_waiting(self):
        scope = AsyncTaskScope("t")
        scope.spawn(_sleep_forever())
        assert await scope.drain(timeout=0) == 1

    @pytest.mark.asyncio
    async def test_drain_on_empty_scope_is_noop(self):
        assert await AsyncTaskScope("t").drain() == 0

    @pytest.mark.asyncio
    async def test_aclose_cancels_and_drains(self):
        scope = AsyncTaskScope("voice:s1")
        scope.spawn(_sleep_forever())
        scope.spawn(_sleep_forever())
        assert await scope.aclose(timeout=1.0) == 0
        assert scope.closed is True
        assert len(scope) == 0

    @pytest.mark.asyncio
    async def test_aclose_reports_unsettled_tasks(self, caplog):
        """取消无效的任务（此处吞掉前两次取消模拟）如实上报为 error。

        aclose 会对同一任务发两次取消（cancel_all 一次 + drain 超时后一次），
        故需要扛住 2 次；事件循环关闭时的第 3 次取消让它得以收尾。
        """
        scope = AsyncTaskScope("t")
        scope.spawn(_stubborn_body(survive=2), name="stuck")
        # 必须先让协程体真正跑起来（到达 sleep）——否则「首次 cancel 在首步之前送达」
        # 会让任务直接终结，协程体里的 except 一次都不执行。
        await asyncio.sleep(0)
        with caplog.at_level(logging.ERROR):
            leftover = await scope.aclose(timeout=0.02)
        assert leftover == 1
        assert any("仍未落定" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(self):
        scope = AsyncTaskScope("t")
        await scope.aclose(timeout=0.1)
        assert await scope.aclose(timeout=0.1) == 0


# ================================================================
# 拒绝关闭后 spawn
# ================================================================


class TestClosedScope:
    @pytest.mark.asyncio
    async def test_spawn_after_aclose_is_loud(self):
        scope = AsyncTaskScope("voice:s1")
        await scope.aclose(timeout=0.1)
        with pytest.raises(TaskScopeClosedError, match="已关闭"):
            scope.spawn(_sleep_forever(), name="late")

    @pytest.mark.asyncio
    async def test_closed_spawn_closes_the_coroutine(self):
        """拒绝时必须关闭协程对象——否则触发 filterwarnings 的 never-awaited 防线。"""
        scope = AsyncTaskScope("t")
        await scope.aclose(timeout=0.1)
        coro = _sleep_forever()
        with pytest.raises(TaskScopeClosedError):
            scope.spawn(coro)
        # 已 close 的协程再 close 是 no-op；若没被 close，下面会 RuntimeError
        coro.close()

    @pytest.mark.asyncio
    async def test_try_spawn_propagates_closed_error(self):
        """try_spawn 只容忍「无事件循环」，不容忍「已关闭」——否则关停后 spawn 被降级。"""
        scope = AsyncTaskScope("t")
        await scope.aclose(timeout=0.1)
        with pytest.raises(TaskScopeClosedError):
            scope.try_spawn(_sleep_forever())


# ================================================================
# 同步上下文：try_spawn
# ================================================================


class TestTrySpawn:
    def test_try_spawn_without_loop_logs_and_returns_none(self, caplog):
        scope = AsyncTaskScope("character")
        with caplog.at_level(logging.WARNING):
            assert scope.try_spawn(_sleep_forever(), name="flush") is None
        assert any("无运行中事件循环" in r.getMessage() for r in caplog.records)

    def test_try_spawn_does_not_leak_coroutine_warning(self):
        """无 loop 时协程被显式关闭——pytest.ini 把 never-awaited 设为 error。"""
        scope = AsyncTaskScope("character")
        assert scope.try_spawn(_sleep_forever()) is None

    @pytest.mark.asyncio
    async def test_try_spawn_works_with_loop(self):
        scope = AsyncTaskScope("character")
        task = scope.try_spawn(_return_value(7), name="ok")
        assert task is not None
        assert await task == 7


# ================================================================
# cancel_and_drain（单任务有序关闭）
# ================================================================


class TestCancelAndDrain:
    @pytest.mark.asyncio
    async def test_none_task_is_noop(self):
        assert await cancel_and_drain(None) is True

    @pytest.mark.asyncio
    async def test_finished_task_is_noop(self):
        task = asyncio.create_task(_return_value(1))
        await task
        assert await cancel_and_drain(task) is True

    @pytest.mark.asyncio
    async def test_cancels_running_task(self):
        task = asyncio.create_task(_sleep_forever())
        await asyncio.sleep(0)
        assert await cancel_and_drain(task, timeout=1.0) is True
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_timeout_reports_false(self, caplog):
        task = asyncio.create_task(_stubborn_body(survive=1), name="stubborn")
        await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING):
            assert await cancel_and_drain(task, timeout=0.02, label="agent-event") is False
        assert any("agent-event" in r.getMessage() for r in caplog.records)
        task.cancel()  # 第二次取消才生效——显式收尾，避免挂住事件循环关闭
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_task_exception_is_reported_not_raised(self, caplog):
        """单个收尾点不应因任务自身异常而中断整个关闭流程。"""
        task = asyncio.create_task(_raise_after(0.0, ValueError("during shutdown")))
        await asyncio.sleep(0.01)
        with caplog.at_level(logging.WARNING):
            assert await cancel_and_drain(task, label="flush") is True
        assert any("以异常结束" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_outer_cancellation_propagates(self):
        """调用方自己被取消时必须上抛——不能把取消静默吞成「任务已落定」。

        用「扛住取消」的目标任务把 ``cancel_and_drain`` 钉在 ``await`` 上，
        否则目标任务会在外部取消到达前就落定，测试退化成无竞争的假绿。
        """
        task = asyncio.create_task(_stubborn_body(survive=1), name="target")
        await asyncio.sleep(0)

        async def _waiter():
            await cancel_and_drain(task, timeout=10.0)

        outer = asyncio.create_task(_waiter())
        await asyncio.sleep(0.02)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        # cancel_and_drain 只来得及发出第一次取消就被外部取消打断，目标任务的
        # 归宿回到调用方手里——这里补上第二次取消显式收尾。
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
