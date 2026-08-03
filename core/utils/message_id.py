"""Message ID generation."""
from __future__ import annotations

import uuid

from core.schema.types import MessageId


def _new_id(prefix: str) -> str:
    """生成 prefix_ + 26 位十六进制字符（v1 前缀 msg_；批次 2 独立前缀 gen_/seg_，wire 可区分）。"""
    return f"{prefix}_{uuid.uuid4().hex[:26]}"


def new_message_id() -> MessageId:
    """生成新的消息 ID，格式为 'msg_' + 26 位十六进制字符。

    Returns:
        MessageId: 新生成的消息 ID
    """
    return MessageId(_new_id("msg"))


def new_generation_id() -> str:
    """生成一次生成实例 ID（'gen_' 前缀，与 messageId 语义区分，批次 2）。"""
    return _new_id("gen")


def new_segment_id() -> str:
    """生成一个分段 ID（'seg_' 前缀，批次 2）。"""
    return _new_id("seg")
