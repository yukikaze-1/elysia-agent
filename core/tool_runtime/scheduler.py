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

# 工具执行超时 (秒)
_TOOL_TIMEOUT = 60.0


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
        """停止调度器：保存 checkpoint，取消所有 worker，清空队列和注册表。"""
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
        # 排空队列并清空注册表
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        self._tasks.clear()
        logger.info("Scheduler Worker Pool 已停止")

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

        Args:
            request_id: 触发这批工具调用的用户请求 ID，为空时跳过关联
            calls: normalized tool calls from CognitiveLoop

        Returns:
            list of (ToolCall, ToolResult | Exception)
        """
        sub_tasks: list[QueuedToolCall] = []
        for tc in calls:
            t = QueuedToolCall.new_tool_call(
                request_id=request_id or None,
                tool_name=tc.function.name,
                arguments=tc.function.parsed,
                call_id=tc.id,
            )
            sub_tasks.append(t)

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

        try:
            results = await asyncio.gather(
                *[st.get_future() for st in sub_tasks],
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            for st in sub_tasks:
                self.cancel(st.id)
            raise

        # 组装返回: [(tc, result), ...]
        out: list[tuple[ToolCall, ToolResult | BaseException]] = []
        for idx, (tc, st) in enumerate(zip(calls, sub_tasks)):
            raw = results[idx]
            if isinstance(raw, BaseException):
                out.append((tc, ToolResult.fail(
                    name=st.payload.get("tool_name", "?"),
                    error=str(raw),
                )))
            elif isinstance(raw, ToolResult):
                out.append((tc, raw))
            else:
                out.append((tc, raw))
        return out

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

    async def _execute_tool(self, task: QueuedToolCall) -> ToolResult:
        """执行工具子任务，含重试逻辑和超时保护。

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
        while True:
            # 每调用克隆一次 ctx：thread LLM tool_call_id → ctx.call_id（D6-07），
            # 且调用内写 ctx.services 落在副本不污染共享 ctx（对齐 _execute_tools_directly 惯例）
            ctx = self._tool_ctx.clone_for_call(call_id=call_id)
            try:
                result = await asyncio.wait_for(
                    self._tool_executor.execute(
                        tool_name, ctx=ctx, **arguments,
                    ),
                    timeout=_TOOL_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                result = ToolResult.fail(
                    name=tool_name,
                    error=f"执行超时 ({_TOOL_TIMEOUT:.0f}s)",
                    error_type=ErrorType.TIMEOUT,
                )
            except Exception as e:
                result = ToolResult.fail(
                    name=tool_name, error=str(e),
                    error_type=ErrorType.TOOL_ERROR,
                )

            if result.success:
                return result

            # 仅对可重试的错误类型重试；shutdown 期间不重试
            error_type = result.error_type
            retryable = (
                self._running
                and error_type is not None
                and hasattr(error_type, "is_retryable")
                and error_type.is_retryable
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
