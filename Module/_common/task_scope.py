"""按 owner 实例化的后台 asyncio 任务作用域（ARCH-INC-P2-03）。

收敛三件事——后台协程的**所有权**、**异常观察**、**退出收尾**。此前全仓有 40+
处裸 ``asyncio.create_task`` / ``ensure_future``，其中：

- 一部分任务没有任何强引用（可能被 GC 掉）且异常无人观察；
- 同一套「取消 + 限时等待 + 清字段」逻辑在 Agent 生命周期里逐字重复 5 次；
- 同一套「安全包装 + 记日志」在 personality / reflector 里各自实现了一遍。

设计约束（架构评估 §ARCH-INC-P2-03 明确要求）：

- **不做全局万能 Supervisor**。Agent / VoiceSession / CognitiveLoop /
  CharacterEngine / ToolManager 各自持有自己的 :class:`AsyncTaskScope`，生命周期
  随 owner 走。全局单例会让「谁该为这个协程负责」重新变得不可回答。
- **不持久化、不进任务中心、不经 WorkflowEngine**。这里治理的是 *asyncio 协程* 的
  运行时资源，与产品语义的「后台任务」无关——后者在本仓已有专名：
  ``core/tool_runtime``（即时工具调度）、``WorkflowEngine``（长流程）、
  ``Agent._background_tasks`` / ``_BackgroundTaskEntry``（产品任务登记）。
  类名因此**刻意避开**评估稿里的 ``BackgroundTaskSet``：在本仓读作「后台任务集合」
  会直接与 ``_background_tasks`` 撞义。
- **不吞异常**。``spawn()`` 一定挂 done callback；任务以未处理异常结束时记 ERROR，
  而不是等事件循环 GC 时打一句 "Task exception was never retrieved"。

典型用法::

    class VoiceSession:
        def __init__(self, session_id: str) -> None:
            self._tasks = AsyncTaskScope(f"voice:{session_id}")

        def _open(self, ...) -> None:
            self._ack_task = self._tasks.spawn(self._ack_loop(sid), name="voice-ack")

        async def close(self) -> None:
            await self._tasks.aclose(timeout=3.0)   # 取消 + 落定 + 拒绝再 spawn

对「有序关闭」的 owner（如 ``Agent.stop()`` 有明确的逐步关闭顺序），用
:func:`cancel_and_drain` 逐个收尾，最后再让 scope 做一次兜底 drain::

    await cancel_and_drain(self._event_task, label="agent-event")
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

#: ``drain()`` / ``aclose()`` 未显式给 timeout 时使用的默认总预算（秒）
DEFAULT_DRAIN_TIMEOUT = 5.0

#: drain 超时后已请求取消、再等落定的额外预算（秒）
_CANCEL_SETTLE_TIMEOUT = 1.0


class TaskScopeClosedError(RuntimeError):
    """owner 已 ``aclose()`` 之后仍尝试调度后台任务。

    这是**有意的响亮失败**：组件停止后再 spawn，说明存在生命周期顺序错误，
    静默丢弃只会让错误延后到更难定位的地方。
    """


class AsyncTaskScope:
    """一个 owner 持有的后台协程集合。

    职责边界：只做「强引用 + 异常观察 + 批量取消/等待」，不重试、不重启、
    不报告进度、不认识业务语义。任务正常结束即自动移出集合，因此长时间运行
    的 owner 不会累积引用。

    Args:
        owner: 所有者标识，仅用于日志定位（如 ``"agent"``、``"voice:sess-1"``）。
        drain_timeout: 默认 drain 总预算（秒）。
    """

    __slots__ = ("_owner", "_drain_timeout", "_tasks", "_closed")

    def __init__(self, owner: str, *, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT) -> None:
        self._owner = owner
        self._drain_timeout = drain_timeout
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    # ---- 查询 ----

    @property
    def owner(self) -> str:
        """所有者标识（日志用）。"""
        return self._owner

    @property
    def closed(self) -> bool:
        """是否已 ``aclose()``。"""
        return self._closed

    def __len__(self) -> int:
        """当前未结束的任务数。"""
        return sum(1 for t in self._tasks if not t.done())

    def __bool__(self) -> bool:
        return True  # 空 scope 仍为真值——避免 `if scope:` 被误读成「有任务」

    def pending(self) -> tuple[asyncio.Task[Any], ...]:
        """当前未结束的任务快照（调试/测试用）。"""
        return tuple(t for t in self._tasks if not t.done())

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"<AsyncTaskScope {self._owner!r} {state} pending={len(self)}>"

    # ---- 调度 ----

    def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
        log_errors: bool = True,
    ) -> asyncio.Task[Any]:
        """调度一个后台协程并持有强引用。

        Args:
            coro: 要调度的协程对象。
            name: 任务名（日志/调试用），默认 ``"<owner>-task"``。
            log_errors: 任务以未处理异常结束时是否记 ERROR。调用方若会自己
                ``await`` 该任务并处理异常（如 ``Module/tool/manager.py`` 的
                执行任务），可置 False 避免重复噪声——**取消永远不记**。

        Returns:
            调度出的 :class:`asyncio.Task`，调用方需要时可自行持有。

        Raises:
            TaskScopeClosedError: owner 已关闭。
            RuntimeError: 当前没有运行中的事件循环（协程对象会被关闭，不留
                "coroutine was never awaited" 告警）。同步上下文请用
                :meth:`try_spawn`。
        """
        if self._closed:
            coro.close()
            raise TaskScopeClosedError(
                f"作用域已关闭（owner={self._owner}），拒绝再调度后台任务"
                f"（name={name or '<unnamed>'}）")
        task_name = name or f"{self._owner}-task"
        try:
            task = asyncio.get_running_loop().create_task(coro, name=task_name)
        except RuntimeError:
            coro.close()  # 未 awaited 的协程对象必须显式关闭，否则 RuntimeWarning
            raise
        self._tasks.add(task)
        task.add_done_callback(
            self._observe if log_errors else self._forget)
        return task

    def try_spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
        log_errors: bool = True,
    ) -> asyncio.Task[Any] | None:
        """同步上下文友好的 :meth:`spawn`——无运行中事件循环时记 warning 并返回 None。

        供确实可能被同步调用的路径使用（如 ``LLMAdapter.switch_model``、
        ``CharacterRuntimeRegistry.release``）。返回值 None 表示「本轮没调度成」，
        调用方**不应**把它当作成功——这正是从前的静默 ``except RuntimeError: pass``
        要暴露的信息。owner 已关闭时仍然响亮失败。
        """
        try:
            return self.spawn(coro, name=name, log_errors=log_errors)
        except TaskScopeClosedError:
            # TaskScopeClosedError 是 RuntimeError 的子类——必须先于下面拦下，
            # 否则「关停后又 spawn」会被降级成一句 warning。
            raise
        except RuntimeError:  # 仅「无 running loop」这一种情况；coro 已由 spawn 关闭
            logger.warning(
                "作用域 %s 无运行中事件循环，后台任务未调度（name=%s）",
                self._owner, name or "<unnamed>")
            return None

    # ---- 收尾 ----

    def cancel_all(self) -> int:
        """请求取消全部未结束任务，返回实际请求取消的数量（幂等）。"""
        cancelled = 0
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                cancelled += 1
        if cancelled:
            logger.debug("作用域 %s 请求取消 %d 个后台任务", self._owner, cancelled)
        return cancelled

    async def drain(self, *, timeout: float | None = None) -> int:
        """等待全部未结束任务落定，返回**超时前未结束**的任务数。

        与「逐个 wait_for」的写法不同，这里是**总预算**而非「每个任务各给一份
        预算」——N 个任务不会把关闭时间拖成 N × timeout（此前
        ``CognitiveLoop.flush_after_response`` 是逐任务超时，外层又套了一个总超时，
        两层语义不一致，靠后的任务会被外层直接砍掉）。

        超时后取消余下任务并再等 ``_CANCEL_SETTLE_TIMEOUT`` 让其落定；
        仍未落定的（阻塞在不可取消的系统调用里）记 error 并由返回值如实上报。

        Args:
            timeout: 总预算（秒）。``None`` 用作用域默认值；``<=0`` 表示不等待，
                直接取消全部余下任务。
        """
        budget = self._drain_timeout if timeout is None else timeout
        pending = [t for t in self._tasks if not t.done()]
        if not pending:
            return 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(budget, 0.0)
        still_running: set[asyncio.Task[Any]] = set()
        for task in pending:
            # 总预算语义：靠后的任务拿到的是**剩余**时间，而不是各自一份 timeout。
            remaining = deadline - loop.time()
            if remaining > 0:
                await asyncio.wait([task], timeout=remaining)
            if not task.done():
                still_running.add(task)
        if not still_running:
            return 0
        logger.warning(
            "作用域 %s drain 超时（%d/%d 个后台任务未在 %.1fs 内结束），取消余下任务",
            self._owner, len(still_running), len(pending), budget)
        for task in still_running:
            task.cancel()
        # 再给一段**总**预算等待取消落定。不用 asyncio.wait(..., timeout=) ——
        # 它对「取消后仍拒绝结束」的任务会超出超时，把 owner 关闭拖成无界等待。
        deadline_settle = loop.time() + _CANCEL_SETTLE_TIMEOUT
        for task in still_running:
            remaining = deadline_settle - loop.time()
            if remaining > 0:
                await asyncio.wait([task], timeout=remaining)
        stuck = [t for t in still_running if not t.done()]
        if stuck:
            logger.error(
                "作用域 %s 有 %d 个后台任务在取消后仍未落定：%s",
                self._owner, len(stuck), [t.get_name() for t in stuck])
        return len(still_running)

    async def aclose(self, *, timeout: float | None = None) -> int:
        """终止作用域：拒绝后续 spawn + 取消全部 + 等待落定。

        终态 owner（VoiceSession、ToolManager 等一次性生命周期）用这个；
        可重启的 owner（Agent 同一实例 stop→start）用 :meth:`cancel_all` +
        :meth:`drain`，否则重启后会因 :class:`TaskScopeClosedError` 起不来。

        Returns:
            同 :meth:`drain`——超时前未结束的任务数。
        """
        self._closed = True
        self.cancel_all()
        return await self.drain(timeout=timeout)

    # ---- 内部 ----

    def _observe(self, task: asyncio.Task[Any]) -> None:
        """done callback：移出集合 + 上报未处理异常。"""
        self._forget(task)
        if task.cancelled():
            return  # 主动取消是正常收尾路径，不是异常
        exc = task.exception()
        if exc is None:
            return
        logger.error(
            "后台任务未处理异常（owner=%s, task=%s）",
            self._owner, task.get_name(), exc_info=exc)

    def _forget(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)


async def cancel_and_drain(
    task: asyncio.Task[Any] | None,
    *,
    timeout: float = 3.0,
    label: str = "task",
) -> bool:
    """取消**单个**后台任务并等待其落定。

    这是 ``Agent.stop()`` 里此前逐字重复 5 次的「cancel + wait_for + 吞
    CancelledError/TimeoutError + 清字段」的统一实现，用于关闭顺序有强约束、
    必须逐个收尾而不能一次 ``cancel_all`` 的 owner。

    Args:
        task: 目标任务；``None`` 或已完成时立即返回 True（幂等，可重复调用）。
        timeout: 等待落定的预算（秒）。
        label: 日志标签，建议带 owner 前缀（如 ``"agent-event"``）。

    Returns:
        True 表示任务已在预算内落定（含被取消），False 表示超时仍在跑。
    """
    if task is None:
        return True
    if not task.done():
        task.cancel()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.CancelledError:
        # CancelledError 有两个来源，必须区分：
        #   - 目标任务自身被取消（正常收尾）→ 返回 True；
        #   - **调用方**自己被取消（如 Agent.stop() 中途被取消）→ 必须上抛。
        # 判据是「当前任务是否收到过取消请求」：wait_for 只有在自己的超时
        # 触发时才会 uncancel，外来取消会原样上抛且 cancelling() 仍 > 0。
        # 只认 task.cancelled() 是错的——外取消同样会先把目标任务取消掉。
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            raise
        return True
    except asyncio.TimeoutError:
        logger.warning("后台任务 %s 未在 %.1fs 内落定（已请求取消）", label, timeout)
        return False
    except Exception:
        logger.warning("后台任务 %s 以异常结束", label, exc_info=True)
        return True
    return True
