"""
DEPRECATED: 本模块的类型已迁移至 core/schema/。

请从新位置导入:
  from core.schema.part import TextPart, ImagePart, ...
  from core.schema.message import UserMessage, AssistantMessage, ToolResultMessage
  from core.schema.adapters.message_adapter import content_to_openai, content_text, ...

本文件保留为兼容性 re-export，后续删除。
注意：适配器函数签名已变——接受 list[Part] 而非 list[dict]。
外部 dict 数据先用 parts_from_dicts() 转换。
"""
import warnings

warnings.warn(
    "Module.schema.message_format is deprecated. "
    "Use core.schema.part / core.schema.message / core.schema.adapters instead.",
    DeprecationWarning,
    stacklevel=2,
)

# 兼容性 re-export — 从新位置导入再导出
from core.schema.part import (          # noqa: F401
    TextPart, ImagePart, FilePart, AudioPart, VideoPart,
    PartType, ImageMime, ImageDetail, Part,
)
from core.schema.message import (       # noqa: F401
    UserMessage, AssistantMessage, ToolResultMessage,
    MessageRole, Message,
)
from core.schema.tool import ToolCall, ToolCallFunction  # noqa: F401
from core.schema.adapters.message_adapter import (       # noqa: F401
    parts_from_dicts, content_to_openai, content_text,
    has_image, has_file, has_video, has_audio,
    _resolve_image_source, _to_data_uri,
)
from core.schema.types import IsoTimestamp, now_iso  # noqa: F401

# 兼容旧 ChatMessageV2 → 映射为 Message Union（注意：无 to_dict/from_dict 方法）
ChatMessageV2 = Message  # noqa: F401
