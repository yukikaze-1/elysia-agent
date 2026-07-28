"""Message ID generation."""
from __future__ import annotations

import uuid

from core.schema.types import MessageId


def new_message_id() -> MessageId:
    """生成新的消息 ID，格式为 'msg_' + 26 位十六进制字符。

    Returns:
        MessageId: 新生成的消息 ID
    """
    return MessageId(f"msg_{uuid.uuid4().hex[:26]}")
