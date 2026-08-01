"""TaskSystem 对外依赖协议。

TaskSystem 不依赖 CognitiveLoop 的实现细节（如 EventBus），
只依赖这里定义的抽象协议。CognitiveLoop 重构时只需换实现。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from Module.workflow_engine.schema import ApprovalId, HITLData

logger = logging.getLogger(__name__)


class HITLNotifier(ABC):
    """审批快速通道协议。

    TaskSystem 推审批请求到前端时使用。
    不关心谁在另一端——可能是 EventBus、WebSocket、或未来的任意机制。

    实现者保证：
    - 非阻塞：request_approval / cancel_approval 不应长时间阻塞
    - 不抛异常：异常由实现者自行处理，TaskSystem 不感知
    """

    @abstractmethod
    async def request_approval(self, event_data: HITLData) -> None:
        """推送审批请求到前端收件箱。

        审批进入前端收件箱列表，不弹窗打断用户。
        无超时——用户随时可以处理。

        Args:
            event_data: HITLData（Pydantic model），包含:
                - task_id: TaskId
                - approval_id: ApprovalId
                - agent_id: AgentId
                - node_id: str
                - node_task: str         # 当前节点的目标任务描述
                - tool_name: str
                - params: dict
                - rbac_level: RBACLevel  # 工具权限级别
                - risk_summary: str      # 操作类型、影响范围、参数预览
        """
        ...

    @abstractmethod
    async def cancel_approval(self, approval_ids: list[ApprovalId]) -> None:
        """批量取消已发出的审批请求。

        用于任务取消（该任务所有 pending approval）、
        Worker 终止、Replan 换方案等场景，
        清理前端收件箱中已不需要的审批卡片。

        Args:
            approval_ids: 要取消的审批请求 ID 列表。空列表无操作。
        """
        ...


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
        task_system = TaskSystem(infra, hitl_notifier=hitl)
    """

    # EventBus 事件类型——需要在 core/event_bus.py 中注册
    HITL_REQUEST = "hitl_request"
    HITL_CANCEL = "hitl_cancel"

    def __init__(self, bus: Any) -> None:
        """Initialize the EventBus-based HITL notifier.

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
