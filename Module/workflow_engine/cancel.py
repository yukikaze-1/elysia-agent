"""WorkflowEngine 取消原语——executor/planning 共用的任务级取消检查。

cancel() 已同步置 `WorkflowState.status = CANCELLED`（权威终态），
本原语在任何 await 边界一查即中：已取消 → 抛 CancelledError 统一走
强取消传播链（executor → orchestrator → _run_and_cleanup/_run_resume）。

独立成模块避免 executor.py 的模块私有函数被 planning.py 跨子包引用。
"""

from __future__ import annotations

import asyncio

from Module.workflow_contract import WorkflowStatus
from Module.workflow_engine.schema.internal import WorkflowState


def check_cancelled(ts: WorkflowState) -> None:
    """取消检查——cancel() 已同步置 CANCELLED，await 边界一查即中。

    Args:
        ts: 任务级状态。若 status 为 CANCELLED 则抛 asyncio.CancelledError。

    Raises:
        asyncio.CancelledError: 任务已被取消。
    """
    if ts.status == WorkflowStatus.CANCELLED:
        raise asyncio.CancelledError()
