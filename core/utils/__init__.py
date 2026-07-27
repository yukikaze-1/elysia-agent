"""core/utils/ — 通用工具函数。"""
from .message_id import new_message_id
from .log_setup import setup, set_request_id, get_request_id, log_conversation

__all__ = [
    "new_message_id",
    "setup",
    "set_request_id",
    "get_request_id",
    "log_conversation",
]
