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
    """基于 EventBus 的 HITLNotifier 实现（**当前唯一实现，已注入生产 WorkflowEngine**）。

    用 `EventBus.post()` 推送审批卡片到前端；EventBus 是 Agent 内部的优先级事件通道。

    **S3-23 实测的三处断裂**（原先 docstring 写"演示代码——不直接用于生产"，而它其实
    就是生产装配用的那一个，`_lifecycle_task_system` 直接注入 WorkflowEngine）：

    1. `request_approval` 用 `type="hitl_request"`（本类的常量）post **plain dict**；
       而 `_process_events` 的判别联合里没有这个类型 → 事件被**静默丢弃**
       （已修：`_dispatch_event` 补了 `else` 分支并记 warning + 计数）；
    2. mailbox 的 `approval_needed` 不在 `_poll_mailboxes` 处理的三个 type 里；
    3. 审批结果回传路由到**错误的审批域**（workflow `agent_id` 查
       `SafetyPipeline._hitl_requests`，恒不命中）。

    **v1 不可达**：`needs_approval()` 无条件 `return False`、Evaluator v1 不产
    `ASK_APPROVAL`——所以上述三处目前**没有任何触发路径**，属潜伏缺陷。

    **启用 workflow HITL 之前必须先把 1–3 接通**：本 docstring 就是那条交接说明，
    不要照着旧文案把它当"演示代码"忽略掉。
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
            # S3-23：原注释写"不抛异常——Mailbox 审计流兜底"，但**本类没有 mailbox
            # 兜底**（构造只收 bus）。吞掉异常等于"审批请求悄无声息地消失"。
            # 仍不抛（调用方是 WorkflowEngine，抛会打断任务），但必须**可见**。
            logger.warning(
                "HITLNotifier.request_approval: 审批请求已丢弃（无消费端兜底）"
                " approval_id=%s", event_data.approval_id)

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
