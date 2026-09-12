"""HITL（Human-in-the-Loop）通知器 —— EventBus 演示实现。

HITLNotifier ABC 定义在契约层 Module/workflow_contract/schema.py。
本文件提供基于 EventBus 的演示实现（非真实，后续由 CognitiveLoop 侧替换）。
预留（P3-01-RESERVED）：2026-09-12 决策保留作扩展储备，非死代码，勿按引用审计删除。

由 workflow_engine 分离项目自 Module/workflow_engine/protocol.py 迁入 core 侧。
"""

from __future__ import annotations

import logging
from typing import Any

from Module.workflow_contract import ApprovalId, HITLData, HITLNotifier

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 演示实现（非真实，后续由 CognitiveLoop 侧替换）
# ═══════════════════════════════════════════════════════════════════════════════

class EventBusHITLNotifier(HITLNotifier):
    """基于 EventBus 的 HITLNotifier 演示实现。

    用 EventBus.post() 推送审批卡片到前端。
    EventBus 是 Agent 内部的优先级事件通道，前端通过 WebSocket 订阅。

    ⚠️ 演示代码——不直接用于生产。认知循环重构时替换为正式实现。

    用法:
        bus = EventBus()
        hitl = EventBusHITLNotifier(bus)
        task_system = WorkflowEngine(infra, hitl_notifier=hitl)
    """

    # EventBus 事件类型——需要在 core/event_bus.py 中注册
    HITL_REQUEST = "hitl_request"
    HITL_CANCEL = "hitl_cancel"

    def __init__(self, bus: Any) -> None:
        """初始化基于 EventBus 的 HITL 通知器。

        Args:
            bus: core.event_bus.EventBus 实例。
        """
        self._bus = bus

    async def request_approval(self, event_data: HITLData) -> None:
        """通过 EventBus 推送审批到前端收件箱。

        Args:
            event_data: HITLData 审批事件数据。
        """
        try:
            # 转为 dict 投递到 EventBus，前端通过 WebSocket 订阅
            self._bus.post(
                type=self.HITL_REQUEST,
                data=event_data.model_dump(),
            )
        except Exception:
            logger.exception(
                "HITLNotifier.request_approval 失败: approval_id=%s",
                event_data.approval_id,
            )
            # 不抛异常——Mailbox 审计流兜底

    async def cancel_approval(self, approval_ids: list[ApprovalId]) -> None:
        """通过 EventBus 批量通知前端移除审批卡片。

        Args:
            approval_ids: 要取消的审批 ID 列表。
        """
        if not approval_ids:
            return
        try:
            self._bus.post(
                type=self.HITL_CANCEL,
                data={"approval_ids": [str(aid) for aid in approval_ids]},
            )
        except Exception:
            logger.exception(
                "HITLNotifier.cancel_approval 失败: count=%d",
                len(approval_ids),
            )
            # 不抛异常——前端收件箱有过期检测兜底
