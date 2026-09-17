"""WS tool 域 handler：HITL 审批回传 / Desktop 调用状态。

ARCH-INC-P2-01（批次 D）领域化拆分；详见 `chat.py` 模块说明。
"""

import logging
from typing import Any


from Module.output import WebSocketChannel
from core.input.ws_handlers.context import WsRequestContext

# 复用父模块 logger 名（保持日志可追溯性：日志过滤/告警规则按
# `core.input.ws_handler` 配置，拆包不应改变运维可观测性）
logger = logging.getLogger("core.input.ws_handler")
from core.input._common import _find_channel


async def _dispatch_tool_approval(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    request_id = msg.request_id if msg is not None else data.get("request_id", "")
    approved = msg.approved if msg is not None else data.get("approved", False)
    if request_id:
        # CR-13：审批绑定发起连接——`channel.connection_id` 是服务端在握手时分配的
        # 稳定身份（v2），提交方与发起方不一致时由安全管道拒绝。原先该 handler
        # 拿到 `channel` 却不用，只校验 request_id 是否存在，而该 ID 被广播到所有
        # 通道：多客户端共享产品 token 时，B 可直接批准 A 的高危工具调用。
        approver = getattr(channel, "connection_id", None) if channel is not None else None
        # P1-6：走 ToolAdapter 公开 API（原先直取 ._manager，违反跨模块私有访问纪律）。
        ok = ctx.agent.tool_adapter.submit_approval(
            request_id, approved, approver)
        if ok:
            logger.info("WS 审批结果: request=%s approved=%s", request_id, approved)
        else:
            logger.debug("WS 审批结果: request=%s 不存在或已过期", request_id)

async def _dispatch_tool_status(
    ctx: WsRequestContext, data: dict, msg: Any,
    channel: WebSocketChannel | None,
) -> None:
    # 批次 4（spec §5.3）：桌面工具状态回传 → bridge.handle_status。
    # 仅 v2 连接处理——v1 无 desktop 概念，忽略（含 v1 伪冒 platform 头隔离）。
    # 批次 8a（D2）：发送连接校验——channel.connection_id 作为 sender 传入
    # handle_status（发送连接必须是投递目标）；v2 但无 conn_id（异常）→ 拒绝。
    if ctx.ws is None:
        logger.debug("WS tool.status 无发送连接，忽略")
    else:
        channel = _find_channel(ctx.agent, ctx.ws)
        if channel is None or channel.protocol_version < 2:
            logger.debug("WS tool.status: v1 连接忽略")
        elif channel.connection_id is None:
            logger.warning("WS tool.status: channel 无 connection_id，拒绝")
        else:
            # data 已由判别联合校验为合法 ToolStatusMessage——bridge.handle_status
            # 契约接受 wire 帧（camelCase dict），原样透传（既有行为保持）。
            await ctx.agent.desktop_bridge.handle_status(
                data, sender_connection_id=channel.connection_id)

