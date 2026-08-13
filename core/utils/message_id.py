"""消息 ID 生成（薄重导出 shim）。

实现已迁至 core/schema/utils.py（2026-08-14 模块化修复）——ID 生成器返回类型全为
core.schema NewType，属类型权威附属，放 schema 天经地义。本模块保留为薄重导出，
维持既有调用面（``from core.utils.message_id import ...`` / ``from core.utils import ...``）
不破坏；core.utils → core.schema 单向依赖，符合「叶子在最底」。
"""

from core.schema.utils import (
    new_attachment_id,
    new_generation_id,
    new_memory_id,
    new_message_id,
    new_segment_id,
    new_task_id,
    stable_tool_message_id,
)

__all__ = [
    "new_attachment_id",
    "new_generation_id",
    "new_memory_id",
    "new_message_id",
    "new_segment_id",
    "new_task_id",
    "stable_tool_message_id",
]
