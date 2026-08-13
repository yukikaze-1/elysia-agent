"""core/schema 基础类型 — 时间戳、标识符别名。"""

from typing import Annotated, NewType

from pydantic import BeforeValidator

from Module.workflow_contract.schema import SessionId


def _normalize_iso_ts(v: str | float) -> str:
    """归一化 ISO 时间戳（lazy wrapper）——打破 core.schema.utils ↔ types 包内环。

    core.schema.utils 顶层 eager import core.schema.types（ID 生成器运行时需 NewType
    类构造），故 types 侧对本 helper 仅运行时引用（Pydantic BeforeValidator 构造时解析，
    2026-08-14 模块化修复）。
    """
    from core.schema.utils import _normalize_iso_ts as _impl

    return _impl(v)


IsoTimestamp = Annotated[str, BeforeValidator(_normalize_iso_ts)]
"""ISO 8601 时间戳，构造时自动校验格式并归一化到 UTC。

使用 Pydantic BeforeValidator：空字符串和格式错误会抛出 ValidationError。
"""

# SessionId 由契约层（Module.workflow_contract.schema）拥有，core 侧重导出保持公开 API。
# 运行时即 str（NewType），与历史定义完全兼容。契约层不反向依赖 core。

MessageId = NewType("MessageId", str)
"""消息唯一标识符。"""

ToolCallId = NewType("ToolCallId", str)
"""工具调用唯一标识符。"""

GenerationId = NewType("GenerationId", str)
"""生成实例唯一标识符（gen_ 前缀）。"""

SegmentId = NewType("SegmentId", str)
"""生成分段唯一标识符（seg_ 前缀）。"""

RequestId = NewType("RequestId", str)
"""请求唯一标识符（`{_req_counter:03d}` 三位计数器格式，非 UUID——与 server.py:132 实现一致）。"""

AttachmentId = NewType("AttachmentId", str)  # 附件 ID（att_ 前缀）
MemoryId = NewType("MemoryId", str)          # LTM 记忆条目 ID（new_message_id 值空间）
TaskId = NewType("TaskId", str)              # 调度中心任务 ID
