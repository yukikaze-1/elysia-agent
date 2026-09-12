"""core/input 跨业务共享通用件（2026-08-14 批次 B 拆分）。

_find_channel / _send_ack 原在 ws_handler.py，被 v2 深业务（ws_v2）与协议层（ws_handler 保留
分支）共用。下沉本模块避免 ws_v2 ↔ ws_handler 循环（Q3：跨业务共享进 _common）。
"""

import logging
import uuid
from typing import TYPE_CHECKING

from aiohttp import web

from core.schema.message import Message, character_snapshot_to_wire
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

    经 `OutputManager.channels` / `WebSocketChannel.ws` 公开枚举与比对
    （2026-09-12 前直取 `agent.output._channels` 与 `channel._ws` 私有成员）；
    ws 为 None 时返回 None。
    """
    if ws is None:
        return None
    for ch in agent.output.channels:
        if isinstance(ch, WebSocketChannel) and ch.ws is ws:
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


def _archive_to_client(msgs: list[Message]) -> list[dict]:
    """将 Message 列表转为前端兼容格式（补 parts + _id 字段）。

    原 api_routes 模块级函数，2026-08-14 批次 C 迁 _common（session_routes/debug_routes/archive
    跨组共享，Q3）。

    Args:
        msgs: Message 对象列表。

    Returns:
        list[dict]: 前端兼容的消息字典列表，每项包含 _id、parts 等字段。
    """
    result = []
    for msg in msgs:
        entry = msg.model_dump(mode="json")
        character = character_snapshot_to_wire(entry.pop("character", None))
        if character is not None:
            entry["character"] = character
        mongo_id = entry.get("metadata", {}).pop("_mongo_id", None)
        if mongo_id:
            entry["_id"] = mongo_id
        content = entry.get("content", [])
        if isinstance(content, list):
            entry["parts"] = [
                {"content": p["content"]}
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
        elif isinstance(content, str):
            entry["parts"] = [{"content": content}]
        else:
            entry["parts"] = [{"content": ""}]
        result.append(entry)
    return result


def _archive_msg_to_contract(d: dict) -> rest_contract.Message | None:
    """客户端归档消息 dict → 契约 Message 视图（block_index → content_type 归一化）。

    保留 _archive_to_client 的全部输出键（parts/_id/timestamp 等，前端分页游标与渲染依赖），
    仅重命名 block_index 键对齐 session 回放 wire（SessionMessageItem.content_type）。
    单条校验失败返回 None（调用方丢弃该条并告警，避免整页归档加载失败——与 Task 3 M2
    逐条契约化兜底同一策略）。
    """
    wire = dict(d)
    if "block_index" in wire:
        wire["content_type"] = wire.pop("block_index")
    try:
        return rest_contract.Message.model_validate(wire)
    except Exception:
        logger.warning("Failed to contract-validate archive message: %s", d.get("message_id", "?"))
        return None
