"""core/utils/ — 通用工具函数。"""
from .message_id import (
    new_attachment_id,
    new_generation_id,
    new_memory_id,
    new_message_id,
    new_segment_id,
    new_task_id,
)
from .log_setup import setup, set_request_id, get_request_id, log_conversation
from .text_utils import content_text_from_content, MessageObj

__all__ = [
    "new_attachment_id",
    "new_generation_id",
    "new_memory_id",
    "new_message_id",
    "new_segment_id",
    "new_task_id",
    "setup",
    "set_request_id",
    "get_request_id",
    "log_conversation",
    "content_text_from_content",
    "MessageObj",
]
