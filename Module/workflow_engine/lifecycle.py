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
) -> bool:
    """软取消任务。幂等。

    PAUSED 分支在内部完成 cleanup_task() + cleanup_storage()
    + semaphore.release()。调用方不得再次释放。

    二次确认由 CognitiveLoop 在上层处理。

    幂等——所有路径返回 True：
    - RUNNING → 设置 exec_state.cancel_requested → Orchestrator 检测 → CANCELLED
    - PAUSED  → 直接标记 CANCELLED，已有 checkpoint
    - 已终态   → True（无需操作）
    - 已清理   → True（任务已不存在）

    Args:
        tasks: 所有活跃任务的 SubAgent 映射。
        mailboxes: 所有活跃任务的事件邮箱映射。
        semaphore: 并发控制信号量，PAUSED 分支需 release()。
        cleanup_task: 清理任务注册表回调。
        cleanup_storage: 清理存储（checkpoint + artifact）回调。
        task_id: 要取消的任务 ID。

    Returns:
        True: 取消信号已发送（幂等，已终态/已清理也返回 True）。
    """
    # TODO: v1 只设标志，无超时反馈。
    #   若 Orchestrator 因异常永远不到达下一个循环迭代，
    #   cancel 信号可能永远不被处理。后续考虑 await + 超时机制。
    sub_agent = tasks.get(task_id)
    if sub_agent is None:
        return True  # 已清理 = 已取消

    ts = sub_agent._task_state
    if ts is None or ts.is_terminal():
        return True  # 已终态 = 已取消

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
        # 此处必须显式清理注册表 + storage + semaphore，否则永久泄漏。
        cleanup_task(task_id)
        try:
            await cleanup_storage(task_id, sub_agent)
        except Exception:
            logger.exception(
                "lifecycle: task_id=%s cancel(PAUSED) 存储清理失败", task_id,
            )
        if semaphore:
            semaphore.release()
        return True

    # ── 分支 2: RUNNING / PLANNING → 设取消标志 ──
    if sub_agent._orchestrator is None:
        ts.status = WorkflowStatus.CANCELLED
        ts.updated_at = now_iso()
        return True
    exec_state = sub_agent._orchestrator._exec_state
    if exec_state is None:
        # 尚未进入 Orchestrator（规划/校验阶段）
        ts.status = WorkflowStatus.CANCELLED
        ts.updated_at = now_iso()
        return True

    exec_state.cancel_requested = True
    return True


def submit_approval(
    tool_registry: object,  # ToolManager 实例
    agent_id: str,
    approved: bool,
) -> bool:
    """审批结果回传。v1 桥接到 ToolManager 的 SafetyPipeline。

    将 agent_id 作为 request_id 委托给 tool_registry.submit_approval()。
    ToolManager 内部 SafetyPipeline 设置 asyncio.Event 唤醒等待中的工具调用。

    Args:
        tool_registry: ToolManager 实例（runtime 类型，使用 object 避免循环导入）。
        agent_id: 审批请求 ID（用作 request_id）。
        approved: 审批通过为 True，拒绝为 False。

    Returns:
        True:  审批请求已找到并处理
        False: 审批请求不存在（可能已超时或从未创建）
    """
    # tool_registry 运行时是 ToolManager，submit_approval 方法
    # 在 v1 的 SafetyPipeline 路径中始终可用。
    if not hasattr(tool_registry, "submit_approval"):
        return False
    return tool_registry.submit_approval(  # type: ignore[attr-defined]
        request_id=agent_id,
        approved=approved,
    )
