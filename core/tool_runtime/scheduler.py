"""ToolCallDispatcher — 即时工具调用的 Worker Pool（原 TaskScheduler）。

被 CognitiveLoop 调用。管理 N 个 worker 消费工具子任务和后台任务。
Future 双向绑定确保 INTERRUPT 取消时精准传播到 Worker。

**扩展储备（`P3-01-RESERVED`）**：`dispatch_background()`、`submit(QueuedToolCall)`、`get_task()`、
`get_tree()` 与 checkpoint 持久化（`_save_checkpoint`/`_restore_checkpoint`）当前均无
生产消费者（生产主路径是 `dispatch_tool_calls()`），但 2026-09-12 决策**保留作扩展
储备**（非死代码）——勿按引用审计删除。详见 `docs/review/2026-08-30-后端架构分层与
模块化增量评估.md` §ARCH-INC-P1-01「收窄范围」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from core.adapter import ToolContext, ToolResult, ErrorType

from core.schema.tool import ToolCall

from core.schema.types import RequestId, TaskId

from core.tool_runtime.task import QueuedToolCall, QueuedToolCallKind, QueuedToolCallState

if TYPE_CHECKING:
    from core.adapter.tool_adapter import ToolAdapter

logger = logging.getLogger(__name__)

# 工具执行超时 (秒) — **单次尝试**的下限（`_execute_tool` 内层 wait_for 的默认值）。
_TOOL_TIMEOUT = 60.0

# 插件可声明更长的自有预算（`SafetyProfile.request_timeout`）。desktop/vision 按设计
# 声明 **120s**（≥ 桥超时，否则往返会在 30s 被提前取消）；调度器此前一律用 60s 硬切，
# 于是"设计好的 120s 路径"在 60s 被截断（S2-G2）。
#
# 这里声明"我们容许插件声明的最大值"，**由门禁交叉校验**：
# `scripts/check_timeout_budget.py` 的 R4 扫描 `core/tools/**/plugin.py` 的
# `request_timeout=` 实参，任何插件声明超过 `_MAX_PLUGIN_REQUEST_TIMEOUT` 即红——
# 逼着这里同步抬上界，而不是静默截断（这类"两套独立声明、无人校验大小关系"
# 正是 S2-A3/S2-G2/S2-R2-1 的共同成因）。
_MAX_PLUGIN_REQUEST_TIMEOUT = 120.0

# 管道自身开销余量（校验/审批/信号量/结果包装）——外层预算必须比插件预算宽一点，
# 否则会在插件**刚好**用满自己的预算时把收尾切掉。
_TIMEOUT_SLACK = 5.0

# 单次尝试的**上界**：插件声明得再大也不超过它（超出部分由 R4 在门禁层拦下）。
_MAX_TOOL_TIMEOUT = _MAX_PLUGIN_REQUEST_TIMEOUT + _TIMEOUT_SLACK

# 单任务重试上限与退避上限——必须与 `QueuedToolCall` 的默认值/实现保持一致：
# `max_retries` 默认 2（task.py），`retry_backoff_seconds()` 的 cap 为 30s。
_MAX_TASK_RETRIES = 2
_RETRY_BACKOFF_CAP = 30.0

# 单个工具调用从入队到终态的最坏耗时上界（秒）：
#   首发 + max_retries 次重试，各 ≤ _MAX_TOOL_TIMEOUT，外加每次重试前的退避。
# 用 `_MAX_TOOL_TIMEOUT`（而非 `_TOOL_TIMEOUT`）作基数：否则声明了 120s 的插件会在
# 第三次重试中途被整体预算掐断——"外层 < 内层"的老问题换个位置复发。
_CALL_BUDGET = _MAX_TOOL_TIMEOUT * (1 + _MAX_TASK_RETRIES) + _RETRY_BACKOFF_CAP * _MAX_TASK_RETRIES

# `dispatch_tool_calls` 的整体等待上限（秒）。
#
# **必须是有限值，这是活性不变量而非性能调优**：worker pool 未启动、已停止，或有
# worker 静默死亡时，子任务的 future 永远不会被 resolve，而没有预算的 `await gather`
# 会让整个回合**永久挂起**，并一直持有 `CognitiveLoop._process_lock` + 会话 work
# lease——该会话从此无法再接受任何输入（用户侧的"卡死"，无任何日志）。
#
# 取值 = 2 × 单调用最坏预算：
#   - 下界必须 ≥ `_CALL_BUDGET`，否则会掐断**设计内**的单工具最坏路径（3 次尝试）；
#   - 多留一波覆盖 num_workers=5 的常规批量并发；
#   - 更大的批量宁可有界失败并给出明确错误，也不要悬挂。
# `core/tool_runtime/tests/test_task_scheduler.py` 有断言锁死 "≥ _CALL_BUDGET"。
_DISPATCH_TIMEOUT = 2 * _CALL_BUDGET


def _tool_name_of(task: QueuedToolCall) -> str:
    """从任务 payload 取工具名（非 TOOL_CALL 的 payload 可能不含该键）。"""
    payload = task.payload
    if isinstance(payload, dict):
        return str(payload.get("tool_name", "?"))
    return "?"


class ToolCallDispatcher:
    """Worker Pool，执行 AI 规划的工具子任务和后台任务。

    不管理认知任务 — 认知循环有自己的执行路径。
    """

    def __init__(
        self,
        tool_executor: "ToolAdapter | None" = None,
        tool_ctx: ToolContext | None = None,
        num_workers: int = 5,
        checkpoint_path: str | None = None,
    ):
        """初始化调度器。

        Args:
            tool_executor: 工具适配器，实现 ToolExecutor Protocol
            tool_ctx: 工具执行上下文
            num_workers: worker 数量
            checkpoint_path: checkpoint 文件路径
        """
        self._tool_executor = tool_executor
        self._tool_ctx = tool_ctx
        self._num_workers = num_workers
        self._checkpoint_path = checkpoint_path
        self._queue: asyncio.PriorityQueue[tuple[int, int, QueuedToolCall]] = asyncio.PriorityQueue()
        self._tasks: dict[TaskId, QueuedToolCall] = {}
        self._workers: list[asyncio.Task] = []
        self._prune_task: asyncio.Task | None = None
        self._running = False
        self._seq: int = 0

    def set_tool_adapter(self, adapter: "ToolAdapter") -> None:
        """设置工具适配器，用于 Agent.start() 中延迟注入 ToolContext。

        Args:
            adapter: 工具适配器实例
        """
        self._tool_ctx = adapter.tool_context

    # ---- 生命周期 ----

    async def start(self) -> None:
        """启动调度器：恢复 checkpoint，创建 worker 协程和清理协程。"""
        self._running = True
        # 确保 checkpoint 目录存在
        if self._checkpoint_path:
            Path(self._checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        self._restore_checkpoint()
        for i in range(self._num_workers):
            w = asyncio.create_task(self._worker_loop(f"w-{i}"), name=f"scheduler-w-{i}")
            self._workers.append(w)
        # 独立周期性清理任务 — 避免 worker 间竞态
        self._prune_task = asyncio.create_task(self._prune_loop(), name="scheduler-prune")
        logger.info("Scheduler Worker Pool 启动 (%d workers)", self._num_workers)

    async def stop(self) -> None:
        """停止调度器：保存 checkpoint，取消所有 worker，清空队列和注册表。

        S3-02：清空注册表**之前**必须把仍在等待的 future 全部以明确失败 resolve。
        否则 `dispatch_tool_calls` 的 `await gather/wait` 会永久悬挂——被取消的
        worker 只会处理它**已经取到**的那个任务，还留在队列里的子任务其 future
        无人 resolve，而注册表随即被 clear，连条目都不再可见。
        """
        self._running = False
        self._save_checkpoint()
        # 先取消清理任务
        if self._prune_task and not self._prune_task.done():
            self._prune_task.cancel()
        # 取消 worker——worker 处理完当前任务后退出
        for w in self._workers:
            if not w.done():
                w.cancel()
        gathers = [w for w in self._workers]
        if self._prune_task:
            gathers.append(self._prune_task)
        if gathers:
            await asyncio.gather(*gathers, return_exceptions=True)
        self._workers.clear()
        self._prune_task = None
        # 排空队列（这些任务没有 worker 会再取，只能在此收口）
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        abandoned = self._fail_pending_futures(
            "工具调度器已停止，工具未执行或未完成",
            error_type=ErrorType.UNAVAILABLE,
        )
        if abandoned:
            logger.warning("Scheduler: 停止时仍有 %d 个子任务在等待，已按失败收口", abandoned)
        self._tasks.clear()
        logger.info("Scheduler Worker Pool 已停止")

    def _fail_pending_futures(self, error: str, *, error_type: ErrorType) -> int:
        """把注册表中所有未完成的 future 以明确失败 resolve，返回处理条数。

        S3-02：这是"永不悬挂"的兜底。`future.set_result()` 而非 `set_exception()`
        ——等待方（`dispatch_tool_calls`）对异常结果的语义是"工具执行抛异常"，而
        "调度器没跑/被停掉"是**正常的有界失败**，应当以 `ToolResult.fail` 呈现给
        LLM（它能看到错误并可换策略），而不是炸出 traceback。
        """
        abandoned = 0
        for task in self._tasks.values():
            fut = task._future
            if fut is None or fut.done():
                continue
            if not task.is_terminal:
                task.status = QueuedToolCallState.CANCELLED
            task.error = error
            task.touch()
            fut.set_result(ToolResult.fail(
                name=_tool_name_of(task), error=error, error_type=error_type,
            ))
            abandoned += 1
        return abandoned

    # ---- 提交 ----

    async def submit(self, task: QueuedToolCall) -> TaskId:
        """提交任务到 Worker Pool。

        Args:
            task: 要提交的任务

        Returns:
            str: 任务 ID
        """
        self._tasks[task.id] = task
        self._seq += 1
        self._queue.put_nowait((task.priority, self._seq, task))
        logger.debug("Scheduler: 任务入队 %s type=%s pri=%d", task.id, task.type.value, task.priority)
        return task.id

    async def dispatch_tool_calls(
        self, request_id: RequestId, calls: list[ToolCall],
    ) -> list[tuple[ToolCall, ToolResult | BaseException]]:
        """CognitiveLoop 调用：将 tool_calls 创建为子任务，并行执行，返回结果。

        本方法是**有界**的（S3-02）：
        1. 调度器未运行时立即以失败返回，不入队（不入队的任务不会有 worker 取）；
        2. 等待受 `_DISPATCH_TIMEOUT` 约束，预算耗尽 → 取消在途子任务 + 逐项明确失败；
        3. 外层取消（用户打断）仍按原语义级联取消子任务后向上抛。

        Args:
            request_id: 触发这批工具调用的用户请求 ID，为空时跳过关联
            calls: normalized tool calls from CognitiveLoop

        Returns:
            list of (ToolCall, ToolResult | Exception)
        """
        # S3-02 守卫：未运行 = 没有 worker 会消费队列。此前这里照常入队并 await，
        # 于是 future 永不 resolve → 回合永久挂起且持 `_process_lock`（会话死锁）。
        if not self._running:
            logger.error(
                "Scheduler: 未运行仍收到 dispatch（%d 个工具调用）→ 立即失败，不入队", len(calls))
            return [
                (tc, ToolResult.fail(
                    name=tc.function.name,
                    error="工具调度器未运行（Agent 未启动或正在关停），工具未执行",
                    error_type=ErrorType.UNAVAILABLE,
                ))
                for tc in calls
            ]

        sub_tasks: list[QueuedToolCall] = []
        for tc in calls:
            t = QueuedToolCall.new_tool_call(
                request_id=request_id or None,
                tool_name=tc.function.name,
                arguments=tc.function.parsed,
                call_id=tc.id,
            )
            sub_tasks.append(t)

        if not sub_tasks:
            return []

        # 绑定 children
        if request_id:
            # 死代码：request_id 是 RequestId 查 TaskId 字典恒不命中，本批不改行为
            parent = self._tasks.get(request_id)  # type: ignore[arg-type]
            if parent is not None:
                for st in sub_tasks:
                    parent.add_child(st.id)

        for st in sub_tasks:
            self._tasks[st.id] = st
            self._seq += 1
            self._queue.put_nowait((st.priority, self._seq, st))

        logger.info("Scheduler: dispatch %d 工具子任务 (request=%s)", len(sub_tasks), request_id or "-")

        futures = [st.get_future() for st in sub_tasks]
        # 用 asyncio.wait 而非 wait_for(gather(...))：wait_for 超时会**级联取消**子
        # future，之后就再也无法把它们改写成明确的失败结果（只能得到空 message 的
        # CancelledError），LLM 侧看到的是"失败但不知为何"。
        try:
            _, pending = await asyncio.wait(futures, timeout=_DISPATCH_TIMEOUT)
        except asyncio.CancelledError:
            for st in sub_tasks:
                self.cancel(st.id)
            raise

        stuck: set[int] = set()
        if pending:
            for idx, fut in enumerate(futures):
                if fut in pending:
                    stuck.add(idx)
            logger.error(
                "Scheduler: dispatch 等待预算耗尽（%.0fs），%d/%d 个子任务未完成 —— "
                "取消并逐项以超时收口: %s",
                _DISPATCH_TIMEOUT, len(stuck), len(sub_tasks),
                [sub_tasks[i].id for i in sorted(stuck)],
            )
            for idx in stuck:
                self.cancel(sub_tasks[idx].id)

        # 组装返回: [(tc, result), ...]
        out: list[tuple[ToolCall, ToolResult | BaseException]] = []
        for idx, (tc, st) in enumerate(zip(calls, sub_tasks)):
            if idx in stuck:
                out.append((tc, ToolResult.fail(
                    name=tc.function.name,
                    error=f"工具调度等待超时（>{_DISPATCH_TIMEOUT:.0f}s），已取消",
                    error_type=ErrorType.TIMEOUT,
                    # 同样是**合成的**超时，必须如实标注来源：今天这条结果不会回到重试循环
                    # （任务已被取消），但"数据说的来源"必须与事实一致——否则将来有人把它
                    # 接回重试路径时，门控会以为这是插件自己的陈述而放行。
                    synthesised_timeout=True,
                )))
                continue
            raw = self._outcome_of(futures[idx], st)
            out.append((tc, raw))
        return out

    @staticmethod
    def _outcome_of(fut: asyncio.Future, task: QueuedToolCall) -> ToolResult:
        """把 future 的终态翻译成 `ToolResult`。

        S3-02 之前取消路径统一走 `gather(return_exceptions=True)` → 得到裸
        `CancelledError`，随后被 `str(raw)` 转成**空字符串**，于是用户/LLM 只看到
        "工具失败：␀"（典型"失败语义静默"）。这里为取消补上可读原因。

        与改动前一致：本方法**恒**返回 `ToolResult`（绝不把裸异常交给消费方——
        `CognitiveToolPhase._dispatch_tool_calls` 会直接取 `.llm_content`）。
        """
        if fut.cancelled():
            return ToolResult.fail(
                name=_tool_name_of(task),
                error="工具调用被取消（回合被打断或调度器已停止）",
                error_type=ErrorType.CANCELLED,
            )
        exc = fut.exception()
        if exc is not None:
            # 形如空 message 的异常（CancelledError/TimeoutError 裸实例）也给出类型名，
            # 避免 `str(exc) == ""` 把失败原因整个吞掉。
            detail = str(exc) or type(exc).__name__
            return ToolResult.fail(
                name=_tool_name_of(task), error=detail, error_type=ErrorType.TOOL_ERROR)
        raw = fut.result()
        if isinstance(raw, BaseException):
            return ToolResult.fail(
                name=_tool_name_of(task),
                error=str(raw) or type(raw).__name__,
                error_type=ErrorType.TOOL_ERROR,
            )
        if isinstance(raw, ToolResult):
            return raw
        return ToolResult(success=True, content=str(raw), tool_name=_tool_name_of(task))

    async def dispatch_background(self, coro: Any) -> TaskId:
        """提交一个后台协程，返回 task_id（P3-01-RESERVED：无生产调用方，保留作扩展储备）。

        Args:
            coro: 要作为后台任务执行的协程

        Returns:
            str: 后台任务的 ID
        """
        t = QueuedToolCall.new_background(payload={"coro": coro})
        self._tasks[t.id] = t
        self._seq += 1
        await self._queue.put((t.priority, self._seq, t))
        logger.info("Background task dispatched: %s", t.id)
        return t.id

    def cancel(self, task_id: TaskId, _visited: set | None = None) -> bool:
        """取消任务 — 级联取消子任务 + 取消 Worker 执行 + 取消 Future。

        Args:
            task_id: 要取消的任务 ID
            _visited: 内部使用，用于循环检测的已访问集合

        Returns:
            bool: 是否成功取消
        """
        if _visited is None:
            _visited = set()
        if task_id in _visited:
            return False
        _visited.add(task_id)

        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.status = QueuedToolCallState.CANCELLED
        task.touch()

        if task._worker_task is not None and not task._worker_task.done():
            task._worker_task.cancel()

        if task._future is not None and not task._future.done():
            task._future.cancel()

        # 级联取消子树（含循环检测）
        for child_id in task.children_ids:
            self.cancel(child_id, _visited)

        del self._tasks[task_id]
        logger.debug("Scheduler: 任务取消 %s (type=%s)", task_id, task.type.value)
        return True

    # ---- 查询 ----

    def get_task(self, task_id: TaskId) -> QueuedToolCall | None:
        """按 ID 获取任务。

        Args:
            task_id: 任务 ID

        Returns:
            QueuedToolCall | None: 找到的任务，不存在则返回 None
        """
        return self._tasks.get(task_id)

    def get_tree(self, task_id: TaskId) -> dict[str, Any]:
        """递归构建任务树，给 /debug 面板。

        Args:
            task_id: 根任务 ID

        Returns:
            dict[str, Any]: 递归嵌套的任务树字典，不存在则返回空字典
        """
        task = self._tasks.get(task_id)
        if task is None:
            return {}
        tree: dict[str, Any] = {
            "id": task.id,
            "type": task.type.value,
            "status": task.status.value,
            "priority": task.priority,
            "payload": task.payload,
            "error": task.error,
            "retry_count": task.retry_count,
            "progress": task.progress,
        }
        if task.children_ids:
            tree["children"] = [self.get_tree(cid) for cid in task.children_ids]
        return tree

    def all_tasks(self) -> list[QueuedToolCall]:
        """返回所有已知任务的列表。

        Returns:
            list[QueuedToolCall]: 当前注册表中的全部任务
        """
        return list(self._tasks.values())

    @property
    def pending_count(self) -> int:
        """返回待处理任务数量。"""
        return sum(1 for t in self._tasks.values() if t.status == QueuedToolCallState.PENDING)

    @property
    def running_count(self) -> int:
        """返回正在执行的任务数量。"""
        return sum(1 for t in self._tasks.values() if t.status == QueuedToolCallState.DISPATCHED)

    # ---- Worker Loop ----

    async def _worker_loop(self, name: str) -> None:
        """Worker 主循环：从队列取任务，执行，处理结果。

        Args:
            name: worker 名称，用于日志标识
        """
        logger.debug("Worker %s 启动", name)
        while self._running:
            try:
                _, __, task = await self._queue.get()
            except RuntimeError:
                break

            if task.status == QueuedToolCallState.CANCELLED:
                self._queue.task_done()
                self._cleanup_completed(task)
                continue

            task.status = QueuedToolCallState.DISPATCHED
            task._worker_task = asyncio.current_task()
            task.touch()

            try:
                if task.type == QueuedToolCallKind.TOOL_CALL:
                    result = await self._execute_tool(task)
                else:
                    # BACKGROUND 任务 — 若有效载荷中包含协程则执行
                    coro = task.payload.get("coro")
                    if coro is not None and hasattr(coro, "__await__"):
                        result = str(await coro)
                    else:
                        result = task.payload.get("result")
                task.result = result
                task.status = QueuedToolCallState.COMPLETED
                task.touch()
                if task._future is not None and not task._future.done():
                    task._future.set_result(result)
            except asyncio.CancelledError:
                task.status = QueuedToolCallState.CANCELLED
                task.touch()
                if task._future is not None and not task._future.done():
                    task._future.cancel()
            except Exception as e:
                logger.warning("Worker %s: 任务 %s 失败 — %s", name, task.id, e)
                task.error = str(e)
                task.status = QueuedToolCallState.FAILED
                task.touch()
                if task._future is not None and not task._future.done():
                    task._future.set_exception(e)
            finally:
                task._worker_task = None
                self._queue.task_done()
                self._cleanup_completed(task)

            # 清理自身
            if task.is_terminal and not task.children_ids:
                self._tasks.pop(task.id, None)

    async def _prune_loop(self, interval: float = 10.0) -> None:
        """周期性清理终态任务和保存 checkpoint（单线程，无竞态）。"""
        while self._running:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            self._save_checkpoint()
            self._prune_tasks()

    def _prune_tasks(self) -> None:
        """移除终态且无活跃子任务的历史条目，防止 _tasks 无界增长。"""
        active_ids = {tid for tid, t in self._tasks.items() if not t.is_terminal}
        to_remove = []
        for tid, t in self._tasks.items():
            if t.is_terminal and not any(cid in active_ids for cid in t.children_ids):
                to_remove.append(tid)
        for tid in to_remove:
            del self._tasks[tid]
        if to_remove:
            logger.debug("Scheduler: 清理 %d 个终态任务", len(to_remove))

    @staticmethod
    def _cleanup_completed(task: QueuedToolCall) -> None:
        """终态任务移除 worker task 引用。"""
        task._worker_task = None

    def _timeout_for(self, tool_name: str) -> float:
        """单次尝试的超时预算——**尊重插件自己声明的 `request_timeout`**（S2-G2）。

        - 未声明 → `_TOOL_TIMEOUT`（60s，与原先一致）；
        - 声明了 → `插件预算 + 管道余量`，下限 `_TOOL_TIMEOUT`、上限 `_MAX_TOOL_TIMEOUT`。
          desktop/vision 声明 120s → 实际 125s（此前被一律硬切在 60s）。

        **不使用 getattr 兜底**：executor 必须实现 `declared_request_timeout`
        （ToolAdapter 与 ToolControlManager 都有）。缺了就在这里 AttributeError 报出来——
        静默退回 60s 正是本项要消除的"设计好的路径被悄悄截断"。
        """
        executor = self._tool_executor
        if executor is None:
            return _TOOL_TIMEOUT
        declared = executor.declared_request_timeout(tool_name)
        if declared is None:
            return _TOOL_TIMEOUT
        return min(_MAX_TOOL_TIMEOUT, max(_TOOL_TIMEOUT, declared + _TIMEOUT_SLACK))

    def _declares_idempotent(self, tool_name: str) -> bool:
        """插件是否声明"重复调用不产生额外副作用"（`SafetyProfile.idempotent`）。

        只在结果**自带** `synthesised_timeout=True` 时被询问（见 `_execute_tool`）：
        那表示 TIMEOUT 标签是**运行时自己**写的（本方法的 `asyncio.TimeoutError`
        分支，或 `SafetyPipeline.execute` 的内层 `wait_for`），不是插件的陈述；
        插件**返回**的错误照旧按 `ErrorType.is_retryable` 重试。

        与 `_timeout_for` 同一纪律：**不使用 getattr 兜底**——executor 必须实现
        `declares_idempotent`（ToolAdapter 与 ToolControlManager 都有）。缺了就在这里
        AttributeError 报出来；静默兜底成 True 恰好会掩盖本项要消除的"重复副作用"，
        兜底成 False 又会让缺失无声无息。executor 未注入时返回 False（fail-safe，
        该分支此时本就不可达：`_execute_tool` 会先抛 RuntimeError）。
        """
        executor = self._tool_executor
        if executor is None:
            return False
        return executor.declares_idempotent(tool_name)

    async def _execute_tool(self, task: QueuedToolCall) -> ToolResult:
        """执行工具子任务，含重试逻辑和超时保护。

        **重试判定的两个来源**（2026-09-16，勿混淆）：

        - 插件**返回**的 `error_type` 是插件的陈述 → 按 `ErrorType.is_retryable` 重试；
        - **运行时合成**的超时（`result.synthesised_timeout=True`：本方法的
          `asyncio.TimeoutError` 分支、或 `SafetyPipeline.execute` 的内层
          `wait_for(request_timeout)`）是运行时的猜测 → 还要插件声明
          `SafetyProfile.idempotent=True`（`_declares_idempotent`）才重试。

        Args:
            task: 要执行的工具调用任务

        Returns:
            ToolResult: 工具执行结果（成功或失败）
        """
        tool_name = task.payload.get("tool_name", "")
        arguments = task.payload.get("arguments", {})
        call_id = task.payload.get("call_id", "")

        if self._tool_executor is None:
            raise RuntimeError("ToolExecutor 未注入，请先调用 Scheduler.start()")
        if self._tool_ctx is None:
            self._tool_ctx = ToolContext()
        per_call_timeout = self._timeout_for(tool_name)
        while True:
            # 每调用克隆一次 ctx：thread LLM tool_call_id → ctx.call_id（D6-07），
            # 且调用内写 ctx.services 落在副本不污染共享 ctx（对齐 _execute_tools_directly 惯例）
            ctx = self._tool_ctx.clone_for_call(call_id=call_id)
            try:
                result = await asyncio.wait_for(
                    self._tool_executor.execute(
                        tool_name, ctx=ctx, **arguments,
                    ),
                    timeout=per_call_timeout,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # 合成点②：这个 TIMEOUT 是**调度器自己**写的（单次尝试预算
                # `per_call_timeout` 到期），不是插件的陈述——置
                # `synthesised_timeout=True` 让门控不必依赖"这段代码在哪"。
                result = ToolResult.fail(
                    name=tool_name,
                    error=f"执行超时 ({per_call_timeout:.0f}s)",
                    error_type=ErrorType.TIMEOUT,
                    synthesised_timeout=True,
                )
            except Exception as e:
                result = ToolResult.fail(
                    name=tool_name, error=str(e),
                    error_type=ErrorType.TOOL_ERROR,
                )

            if result.success:
                return result

            # 仅对可重试的错误类型重试；shutdown 期间不重试。
            #
            # 区分"谁说的"（2026-09-16）：插件**返回**的 error_type 是插件的陈述，
            # 直接按 is_retryable 走；`result.synthesised_timeout` 为真的那条是
            # **运行时自己**写上的 TIMEOUT（本方法的 `asyncio.TimeoutError` 分支，或
            # `SafetyPipeline.execute` 的内层 `wait_for`）——猜的，只有插件声明
            # `SafetyProfile.idempotent=True`（经 executor.declares_idempotent 问到）
            # 才允许再 invoke 一次，否则有副作用的工具会在这条路径上被重复 invoke。
            #
            # 判据只读**结果上的字段**（不是"哪个分支产生的"）：于是新增合成点只需
            # 置这个标记，不必再往这个表达式里补一条——管道层正是漏在这里。
            # 字段每次尝试由结果自带，无需在循环内复位（上一版靠局部变量，第 1 次
            # 超时、第 2 次返回 NETWORK_ERROR 时必须手动复位才不会串味）。
            error_type = result.error_type
            retryable = (
                self._running
                and error_type is not None
                and hasattr(error_type, "is_retryable")
                and error_type.is_retryable
                and (not result.synthesised_timeout
                     or self._declares_idempotent(tool_name))
                and task.can_retry()
            )
            if not retryable:
                return result

            task.retry_count += 1
            backoff = task.retry_backoff_seconds()
            logger.info(
                "工具 '%s' 失败 (%s)，%d/%d 重试 (%.0fs)",
                tool_name, error_type.value if error_type else "unknown",
                task.retry_count, task.max_retries, backoff,
            )
            await asyncio.sleep(backoff)

    # ---- Checkpoint ----

    def _save_checkpoint(self) -> None:
        """保存 checkpoint：将待处理的后台任务持久化到文件（P3-01-RESERVED）。

        C5/P2-09：
        - 无 pending 时删除旧文件（防陈旧 checkpoint 重启重放已执行任务）；
        - 跳过 payload 含不可序列化值（如协程/future）的后台任务——checkpoint 只
          持久化可重建 descriptor，不序列化运行时对象。
        """
        if not self._checkpoint_path:
            return
        try:
            pending = []
            for t in self._tasks.values():
                if t.status != QueuedToolCallState.PENDING or t.type != QueuedToolCallKind.BACKGROUND:
                    continue
                try:
                    json.dumps(t.payload)  # 可序列化才 checkpoint（排除 coro）
                except TypeError:
                    logger.debug("Checkpoint 跳过不可序列化后台任务 %s", t.id)
                    continue
                pending.append(t.to_dict())
            if not pending:
                # 无待恢复任务 → 删除旧 checkpoint，防陈旧状态重放
                Path(self._checkpoint_path).unlink(missing_ok=True)
                return
            data = {"version": 1, "tasks": pending}
            tmp = self._checkpoint_path + ".tmp"
            Path(self._checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._checkpoint_path)
        except Exception:
            logger.warning("Checkpoint 保存失败", exc_info=True)
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass

    def _restore_checkpoint(self) -> None:
        """恢复 checkpoint：从文件加载待处理任务并重新入队。

        C5/P2-09：版本/schema 校验——损坏或不兼容文件隔离（记 warning 不阻断启动）。
        """
        if not self._checkpoint_path:
            return
        path = Path(self._checkpoint_path)
        # 清理上次崩溃可能残留的 .tmp 文件
        tmp_path = Path(self._checkpoint_path + ".tmp")
        if tmp_path.is_file():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or data.get("version") != 1:
                logger.warning("Checkpoint 版本不兼容/损坏，隔离不恢复")
                return
            tasks_raw = data.get("tasks", [])
            if not isinstance(tasks_raw, list):
                raise ValueError("checkpoint tasks 不是数组")
            for td in tasks_raw:
                t = QueuedToolCall.from_dict(td)
                t.status = QueuedToolCallState.PENDING
                self._tasks[t.id] = t
                self._seq += 1
                self._queue.put_nowait((t.priority, self._seq, t))
            logger.info("Checkpoint 恢复: %d 个任务", len(tasks_raw))
        except Exception:
            logger.warning("Checkpoint 恢复失败（隔离不阻断启动）", exc_info=True)

    # ----

    @property
    def total_count(self) -> int:
        """返回注册表中的任务总数。"""
        return len(self._tasks)

    def __repr__(self) -> str:
        return (f"ToolCallDispatcher(pending={self.pending_count}, "
                f"running={self.running_count}, total={self.total_count})")
