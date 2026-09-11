"""Debug routes 本地共享 helper（CORE-MOD-P2-03）。

debug_routes 不再 import 父包 core.input.*：错误信封与归档转换在此维护本地副本。
父包 `core/input._common` 中同名 helper 供 session_routes/chat_routes 使用；行为必须
保持一致（测试覆盖两条 REST 路径同一 wire 形状）。
"""

import logging
import uuid

from aiohttp import web

from core.schema.message import Message, character_snapshot_to_wire
from frontend_contract.rest import ProtocolError, Message as ContractMessage

logger = logging.getLogger(__name__)


def _protocol_error_response(code: str, message: str, status: int = 401,
                             retryable: bool = False) -> web.Response:
    """构造统一错误信封响应（code/message/retryable/requestId/error/details）。"""
    env = ProtocolError(
        code=code, message=message, retryable=retryable,
        requestId=f"req_{uuid.uuid4().hex[:24]}", error=message,
    ).wire_dict()
    return web.json_response(env, status=status)


def _archive_to_client(msgs: list[Message]) -> list[dict]:
    """将 Message 列表转为前端兼容格式（补 parts + _id 字段）。

    P2-2：character 快照转换必须与 `core/input/_common._archive_to_client` 一致——
    快照经 `character_snapshot_to_wire` 译为 camelCase wire，None 时省略该键
    （不写 `character: null`）。此前本副本漏掉该步，导致 /debug/api/archive 与业务
    REST 的归档消息 wire 形状漂移（内部 snake_case + 空键）。
    依赖 `core.schema.message`（非父包 core.input.*），保持 debug_routes 的解耦约束。
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


def _archive_msg_to_contract(d: dict) -> ContractMessage | None:
    """客户端归档消息 dict → 契约 Message 视图（block_index → content_type 归一化）。"""
    wire = dict(d)
    if "block_index" in wire:
        wire["content_type"] = wire.pop("block_index")
    try:
        return ContractMessage.model_validate(wire)
    except Exception:
        logger.warning("Failed to contract-validate archive message: %s", d.get("message_id", "?"))
        return None
