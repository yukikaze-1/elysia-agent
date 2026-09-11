"""WorkflowEngine 任务操作函数 —— pause / cancel / submit_approval。

所有函数接受显式参数替代 WorkflowEngine 内部状态，
供 WorkflowEngine 方法薄封装委托。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from Module.workflow_engine.mailbox import push_mailbox_event
from Module.workflow_contract import (
    WorkflowEventType,
    WorkflowId,
    WorkflowStatus,
)
from Module.workflow_engine.schema import now_iso

if TYPE_CHECKING:
    from Module.workflow_engine.core.sub_agent import SubAgent
    from Module.workflow_engine.mailbox import WorkflowMailbox

logger = logging.getLogger(__name__)


async def pause(
    tasks: dict[WorkflowId, SubAgent],
    task_id: WorkflowId,
    timeout: float = 30.0,
) -> bool:
    """暂停任务——在下一个安全点停止执行并保存 checkpoint。幂等。

    设 pause_requested 标志后，轮询等待 orchestrator 确认 PAUSED 状态。

    幂等——无论任务处于什么状态都不会报错：
    - RUNNING → 设置 pause_requested 标志，等待 PAUSED 确认
    - PAUSED → 立即返回 True（幂等）
    - 已终态 / 已清理 / 尚未启动 → 返回 False

    Orchestrator 在当前节点执行完成后检测到 pause_requested，
    保存 checkpoint 后返回 PAUSED，SubAgent 推送 PAUSED 事件后退出循环。

    Args:
        tasks: 所有活跃任务的 SubAgent 映射。
        task_id: 要暂停的任务 ID。
        timeout: 等待 PAUSED 确认的超时秒数。

    Returns:
        True:  暂停已确认（task_state.status == PAUSED）
        False: 超时——orchestrator 在 timeout 秒内未到达检查点
                （可能正在执行长 LLM 调用，稍后可重试）
    """
    sub_agent = tasks.get(task_id)
    if sub_agent is None:
        return False  # 已清理——无需暂停

    ts = sub_agent._task_state
    if ts is None or ts.is_terminal():
        return False  # 已终态——无需暂停
    if ts.status == WorkflowStatus.PAUSED:
        return True   # 已暂停——幂等

    if sub_agent._orchestrator is None:
        return False  # 尚未启动——无需暂停
    exec_state = sub_agent._orchestrator._exec_state
    if exec_state is None:
        return False  # 尚未启动——无需暂停

    exec_state.pause_requested = True
    logger.info("lifecycle.pause: task_id=%s 暂停请求已发送", task_id)

    # 等待 orchestrator 确认 PAUSED
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ts.status == WorkflowStatus.PAUSED:
            logger.info("lifecycle.pause: task_id=%s 暂停已确认", task_id)
            return True
        # 任务可能在等待期间进入终态（LLM 恰好在检查点前完成）
        if ts.is_terminal():
            logger.info("lifecycle.pause: task_id=%s 在等待期间已进入终态", task_id)
            return False
        await asyncio.sleep(0.1)

    logger.warning("lifecycle.pause: task_id=%s 暂停超时 (%.0fs)", task_id, timeout)
    return False


async def cancel(
    tasks: dict[WorkflowId, SubAgent],
    mailboxes: dict[WorkflowId, WorkflowMailbox],
    semaphore: asyncio.BoundedSemaphore | None,
    cleanup_task: Callable[[WorkflowId], None],
    cleanup_storage: Callable[[WorkflowId, SubAgent], Awaitable[None]],
    task_id: WorkflowId,
    task_futures: dict[WorkflowId, asyncio.Task],
) -> bool:
    """强取消任务。幂等。

    PAUSED 分支在内部完成 cleanup_task() + cleanup_storage()，
    **不 release semaphore**——E2 §5 起 PAUSED 暂停时已释放执行槽，
    此处再 release 构成双重释放（BoundedSemaphore 越界抛 ValueError）。
    调用方不得再次释放。

    QUEUED 分支（E2）同样不 release——排队任务从未 acquire 槽位。

    RUNNING/PLANNING 分支（E3 §3.2 强取消）——**同步置权威终态 CANCELLED**：
        - 取消信号 = 任务状态，不依赖 orchestrator 回写 status（任何阶段 cancel 后恒 CANCELLED）
        - owned.cancel() 打断 in-flight LLM/planner future（await 边界抛 CancelledError）
        - exec_state.cancel_requested 作为循环顶部快速路径（仅首次 RUNNING 有效）
        - 该分支不推 mailbox 事件——CancelledError 传播链统一由 _run_and_cleanup 推（§3.4）

    二次确认由 CognitiveLoop 在上层处理。

    幂等——所有路径返回 True：
    - RUNNING / PLANNING → 同步置 CANCELLED + owned.cancel() + 快速路径标志
    - PAUSED  → 直接标记 CANCELLED，已有 checkpoint
    - QUEUED  → 直接标记 CANCELLED，未消耗槽位
    - 已终态   → True（无需操作）
    - 已清理   → True（任务已不存在）

    Args:
        tasks: 所有活跃任务的 SubAgent 映射。
        mailboxes: 所有活跃任务的事件邮箱映射。
        semaphore: 并发控制信号量（E2 起 cancel 各分支均不 release）。
        cleanup_task: 清理任务注册表回调。
        cleanup_storage: 清理存储（checkpoint + artifact）回调。
        task_id: 要取消的任务 ID。
        task_futures: 活跃任务的 asyncio.Task 映射（E3 强取消——owned.cancel()）。

    Returns:
        True: 取消信号已发送（幂等，已终态/已清理也返回 True）。
    """
    sub_agent = tasks.get(task_id)
    if sub_agent is None:
        return True  # 已清理 = 已取消

    ts = sub_agent._task_state
    if ts is None or ts.is_terminal():
        return True  # 已终态 = 已取消

    # ── 分支 0: QUEUED → 直接 CANCELLED（排队中取消，从未 acquire 槽位）──
    if ts.status == WorkflowStatus.QUEUED:
        ts.status = WorkflowStatus.CANCELLED
        ts.updated_at = now_iso()
        mailbox = mailboxes.get(task_id)
        if mailbox:
            push_mailbox_event(mailbox, WorkflowEventType.CANCELLED, {
                "task_id": str(task_id),
                "reason": "用户取消",
            })
        cleanup_task(task_id)
        try:
            await cleanup_storage(task_id, sub_agent)
        except Exception:
            logger.exception(
                "lifecycle: task_id=%s cancel(QUEUED) 存储清理失败", task_id,
            )
        return True

    # ── 分支 1: PAUSED → 直接 CANCELLED ──
    if ts.status == WorkflowStatus.PAUSED:
        ts.status = WorkflowStatus.CANCELLED
        ts.updated_at = now_iso()
        mailbox = mailboxes.get(task_id)
        if mailbox:
            push_mailbox_event(mailbox, WorkflowEventType.CANCELLED, {
                "task_id": str(task_id),
                "reason": "用户取消",
            })
        # PAUSED 时 _run_and_cleanup 的 finally 已跳过清理（early return），
        # 此处必须显式清理注册表 + storage，否则永久泄漏。
        # 不 release semaphore——E2 §5 起 PAUSED 暂停时已释放执行槽（防双重释放）。
        cleanup_task(task_id)
        try:
            await cleanup_storage(task_id, sub_agent)
        except Exception:
            logger.exception(
                "lifecycle: task_id=%s cancel(PAUSED) 存储清理失败", task_id,
            )
        return True

    # ── 分支 2: RUNNING / PLANNING → 强取消（E3 §3.2）──
    # 单同步块（无 await）：① 同步置权威终态 → ② exec_state 快速路径 → ③ owned.cancel()。
    # is_terminal() 已覆盖 COMPLETED/FAILED/CANCELLED——与 finalize 竞争的
    # 「终态写入已原子发生则不覆盖」由 is_terminal 短路保证（§3.5 顺序不变量）。
    if ts.is_terminal():
        return True                            # 已终态 = 已取消（幂等）
    ts.status = WorkflowStatus.CANCELLED       # ① 同步置权威终态（PLANNING/RUNNING 恒可达）
    ts.updated_at = now_iso()
    if sub_agent._orchestrator is not None:
        exec_state = sub_agent._orchestrator._exec_state
        if exec_state is not None:             # ② 保留 None 守卫（planning 阶段 _exec_state 未建）
            exec_state.cancel_requested = True  #    循环顶部快速路径
    owned = task_futures.get(task_id)          # ③ owned.cancel() 打断 in-flight await（强取消）
    if owned is not None and not owned.done():
        owned.cancel()
    return True


def submit_approval(
    tool_registry: object,  # ToolAdapter（core）或 ToolManager 实例
    agent_id: str,
    approved: bool,
) -> bool:
    """审批结果回传。v1 桥接到工具子系统的 SafetyPipeline。

    将 agent_id 作为 request_id 委托给 tool_registry.submit_approval()。
    底层 SafetyPipeline 设置 asyncio.Event 唤醒等待中的工具调用。

    P1-6：`AgentInfra.tool_registry` 生产注入的是 `core.adapter.tool_adapter.ToolAdapter`
    （不是 ToolManager），故 ToolAdapter 必须同样公开 `submit_approval` 委托方法——
    否则本函数的 hasattr 判定恒 False、`WorkflowEngine.submit_approval` 成为死路径。

    Args:
        tool_registry: ToolAdapter 或 ToolManager 实例（runtime 类型，用 object 避免循环导入）。
        agent_id: 审批请求 ID（用作 request_id）。
        approved: 审批通过为 True，拒绝为 False。

    Returns:
        True:  审批请求已找到并处理
        False: 审批请求不存在（可能已超时或从未创建）
    """
    # tool_registry 运行时是 ToolAdapter（生产）或 ToolManager；两者均公开
    # submit_approval（见 ToolAdapter.submit_approval，P1-6），v1 SafetyPipeline 路径可用。
    if not hasattr(tool_registry, "submit_approval"):
        return False
    return tool_registry.submit_approval(  # type: ignore[attr-defined]
        request_id=agent_id,
        approved=approved,
    )
