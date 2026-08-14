"""core/input 跨业务共享通用件（2026-08-14 批次 B 拆分）。

_find_channel / _send_ack 原在 ws_handler.py，被 v2 深业务（ws_v2）与协议层（ws_handler 保留
分支）共用。下沉本模块避免 ws_v2 ↔ ws_handler 循环（Q3：跨业务共享进 _common）。
"""

import logging
import uuid
from typing import TYPE_CHECKING

from aiohttp import web

from core.schema.utils import now_iso
from frontend_contract import (
    AcceptedUserAckMessage,
    AckError,
    RejectedUserAckMessage,
)
from frontend_contract import rest as rest_contract
from Module.output import WebSocketChannel

if TYPE_CHECKING:
    from core.agent import Agent

logger = logging.getLogger(__name__)


def _find_channel(agent: "Agent", ws: "web.WebSocketResponse | None") -> "WebSocketChannel | None":
    """按 ws 找到对应 WebSocketChannel（用于读取 protocol_version）。

    仿 switch_model 遍历 agent.output._channels；ws 为 None 或通道不可枚举时返回 None。
    """
    if ws is None:
        return None
    channels = getattr(agent.output, "_channels", None)
    if not isinstance(channels, (list, tuple)):
        return None
    for ch in channels:
        if isinstance(ch, WebSocketChannel) and getattr(ch, "_ws", None) is ws:
            return ch
    return None


async def _send_ack(ws: "web.WebSocketResponse | None", message_id: str,
                    client_request_id: str,
                    client_message_id: str | None, conversation_id: str | None,
                    status: str, error: "dict | AckError | None" = None, *,
                    received_at: str | None = None) -> None:
    """发送 v2 用户消息确认（ws 可空——缺发送连接时静默跳过）。

    Task 6：按 status 构造判别联合成员——accepted → AcceptedUserAckMessage（无 error）；
    rejected → RejectedUserAckMessage（error 经 ``AckError.model_validate`` 归一，兼容
    dict 与 AckError 两种入参，Task 7 传 AckError；rejected 恒 messageId=""）。
    received_at 缺省用 now_iso()；重放路径必须传持久化原值（rejected/accepted 重放
    逐字一致，spec §1.1）。
    """
    if ws is None:
        return
    try:
        ack_ts = received_at or now_iso()
        if status == "accepted":
            ack: AcceptedUserAckMessage | RejectedUserAckMessage = AcceptedUserAckMessage(
                messageId=message_id, clientRequestId=client_request_id,
                clientMessageId=client_message_id, conversationId=conversation_id,
                receivedAt=ack_ts, status="accepted")
        else:
            ack = RejectedUserAckMessage(
                messageId="", clientRequestId=client_request_id,
                clientMessageId=client_message_id, conversationId=conversation_id,
                receivedAt=ack_ts, status="rejected",
                error=AckError.model_validate(error) if error is not None
                else AckError(code="INTERNAL", message="未知错误", retryable=False))
        await ws.send_json(ack.wire_dict())
    except Exception:
        logger.debug("发送 user_message.ack 失败", exc_info=True)


def _tool_error(code: str, message: str, status: int) -> web.Response:
    """统一错误信封（ProtocolError 形状，与 session_routes._error_response 同款）。

    原 api_routes.register_api_routes 内闭包（provider + tools 共用），2026-08-14
    批次 C 下沉 _common.py（跨业务共享，Q3）。

    Args:
        code: 稳定错误码（CALL_NOT_FOUND / INVALID_REQUEST / INTERNAL）。
        message: 公开说明（禁本地路径/参数值，敏感纪律 D-p）。
        status: HTTP 状态码。

    Returns:
        web.Response: ProtocolError 信封 JSON。
    """
    env = rest_contract.ProtocolError(
        code=code, message=message, retryable=False,
        requestId=f"req_{uuid.uuid4().hex[:24]}", error=message,
    ).wire_dict()
    return web.json_response(env, status=status)
