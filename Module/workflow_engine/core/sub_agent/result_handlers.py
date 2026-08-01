"""Orchestrator 结果处理器——SUCCESS / FAILED / CANCELED / BLOCKED / PAUSED。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from Module.workflow_engine.schema import (
    OrchestratorResult,
    WorkflowErrorCode,
    WorkflowEventType,
    WorkflowState,
    now_iso,
)

if TYPE_CHECKING:
    from Module.workflow_engine.checkpoint import CheckpointManager
    from Module.workflow_engine.mailbox import WorkflowMailbox

logger = logging.getLogger(__name__)


async def handle_success(
    result: OrchestratorResult, task_state: WorkflowState,
) -> None:
    """所有 required 节点完成——任务成功。

    Args:
        result: Orchestrator 执行结果
        task_state: 当前任务状态
    """
    logger.info("SubAgent: task_id=%s 成功完成", task_state.task_id)


async def handle_failed(
    result: OrchestratorResult,
    task_state: WorkflowState,
    mailbox: WorkflowMailbox,
    push_mailbox_fn: Callable[..., Awaitable[None]],
) -> None:
    """不可恢复失败——推送 FAILED 事件。

    Args:
        result: Orchestrator 执行结果，包含 error_context
        task_state: 当前任务状态
        mailbox: 任务邮箱，用于推送事件
        push_mailbox_fn: 推送邮箱事件的异步回调
    """
    error_reason = result.error_context.get("reason", "未知")
    logger.error(
        "SubAgent: task_id=%s 失败: %s",
        task_state.task_id, error_reason,
    )
    await push_mailbox_fn(mailbox, WorkflowEventType.FAILED, {
        "task_id": str(task_state.task_id),
        "error": error_reason,
        "error_type": WorkflowErrorCode.INTERNAL_ERROR.value,
    })


async def handle_cancelled(
    result: OrchestratorResult,
    task_state: WorkflowState,
    mailbox: WorkflowMailbox,
    push_mailbox_fn: Callable[..., Awaitable[None]],
) -> None:
    """外部取消——推送 CANCELLED 事件。

    Orchestrator 已将 task_state.status 设为 CANCELLED 并保存了 checkpoint。
    推送事件后退出执行循环，_run_and_cleanup finally 块负责清理。

    Args:
        result: Orchestrator 执行结果
        task_state: 当前任务状态
        mailbox: 任务邮箱，用于推送事件
        push_mailbox_fn: 推送邮箱事件的异步回调
    """
    logger.info("SubAgent: task_id=%s 已取消", task_state.task_id)
    await push_mailbox_fn(mailbox, WorkflowEventType.CANCELLED, {
        "task_id": str(task_state.task_id),
        "reason": "用户取消",
    })


async def handle_blocked(
    result: OrchestratorResult,
    task_state: WorkflowState,
    mailbox: WorkflowMailbox,
    push_mailbox_fn: Callable[..., Awaitable[None]],
) -> None:
    """等待用户审批——推送 APPROVAL_NEEDED 事件。

    Orchestrator 已将 task_state.status 设为 WAITING_APPROVAL。
    返回后由 WorkflowEngine 主类接管：
        1. 等待 HITLNotifier.submit_approval() 回调
        2. 审批通过后从 checkpoint 重建 exec_state → 继续 run()

    Args:
        result: Orchestrator 执行结果
        task_state: 当前任务状态
        mailbox: 任务邮箱，用于推送事件
        push_mailbox_fn: 推送邮箱事件的异步回调
    """
    logger.info("SubAgent: task_id=%s 等待审批", task_state.task_id)
    await push_mailbox_fn(mailbox, WorkflowEventType.APPROVAL_NEEDED, {
        "task_id": str(task_state.task_id),
        "message": "任务等待用户审批",
    })


async def handle_paused(
    result: OrchestratorResult,
    task_state: WorkflowState,
    mailbox: WorkflowMailbox,
    checkpoint_mgr: CheckpointManager,
    push_mailbox_fn: Callable[..., Awaitable[None]],
) -> None:
    """收到暂停请求——保存 checkpoint，推送 PAUSED 事件。

    Args:
        result: Orchestrator 执行结果，包含 checkpoint_state
        task_state: 当前任务状态
        mailbox: 任务邮箱，用于推送事件
        checkpoint_mgr: 检查点管理器，用于持久化 checkpoint
        push_mailbox_fn: 推送邮箱事件的异步回调
    """
    if result.checkpoint_state is None:
        logger.error(
            "SubAgent: task_id=%s PAUSED 但 checkpoint_state 为 None",
            task_state.task_id,
        )
        return
    logger.info(
        "SubAgent: task_id=%s 收到暂停信号，已保存 checkpoint",
        task_state.task_id,
    )
    await checkpoint_mgr.save(
        task_state.task_id, result.checkpoint_state,
    )
    await push_mailbox_fn(mailbox, WorkflowEventType.PAUSED, {
        "task_id": str(task_state.task_id),
        "message": "任务已暂停",
    })
