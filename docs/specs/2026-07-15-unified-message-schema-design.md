# core/schema 统一消息类型设计

> 日期: 2026-07-15 | 状态: design | 作者: yukikaze-1

## 目标

将散落的类型定义统一到 `core/schema/` 目录包中，从消息类型开始逐步迁移。首期：消息、Part、工具调用。

## 目录结构

```
core/schema/                          ← 新建目录包
├── __init__.py                       ← 统一 re-export 所有 public 类型
├── agent.py                          ← 从 core/schema.py 移入，不动
├── types.py                          ← 基础类型（IsoTimestamp, NewType 别名）
├── part.py                           ← Part 类型（Pydantic BaseModel, discriminated union）
├── tool.py                           ← ToolCall 类型
├── message.py                        ← 按 role 拆分的消息类型
└── adapters/
    └── message_adapter.py            ← 格式转换适配器（openai / legacy）
```

同时：
- 删除 `core/schema.py`（内容已移入 `core/schema/agent.py`）
- `Module/schema/message_format.py` 保留为兼容性 re-export（从新位置导入再导出），后续逐步迁移调用方后删除

## 基础类型 — `core/schema/types.py`

```python
from datetime import datetime, timezone
from typing import Annotated, NewType
from pydantic import BeforeValidator

def _normalize_iso_ts(ts: str) -> str:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

def now_iso() -> str:
    """返回当前 UTC 时间 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()

IsoTimestamp = Annotated[str, BeforeValidator(_normalize_iso_ts)]

SessionId = NewType("SessionId", str)
MessageId = NewType("MessageId", str)
ToolCallId = NewType("ToolCallId", str)
```

## Part 类型 — `core/schema/part.py`

全部改为 Pydantic BaseModel，使用 discriminated union：

```python
from enum import StrEnum

class PartType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    AUDIO = "audio"
    VIDEO = "video"

class ImageMime(StrEnum):
    PNG = "image/png"
    JPEG = "image/jpeg"
    GIF = "image/gif"
    WEBP = "image/webp"

class ImageDetail(StrEnum):
    AUTO = "auto"
    LOW = "low"
    HIGH = "high"

class TextPart(BaseModel):
    type: Literal[PartType.TEXT] = PartType.TEXT
    content: str = ""

class ImagePart(BaseModel):
    type: Literal[PartType.IMAGE] = PartType.IMAGE
    src: str = ""                          # URL / data URI / 本地路径
    mime: ImageMime = ImageMime.PNG
    detail: ImageDetail = ImageDetail.AUTO  # OpenAI 视觉模型分辨率控制
    width: int | None = None
    height: int | None = None

class FilePart(BaseModel):
    type: Literal[PartType.FILE] = PartType.FILE
    src: str = ""
    mime: str = "application/octet-stream"
    filename: str = ""
    size_bytes: int = 0

class AudioPart(BaseModel):
    type: Literal[PartType.AUDIO] = PartType.AUDIO
    src: str = ""
    mime: str = "audio/wav"
    duration_ms: int = 0

class VideoPart(BaseModel):
    type: Literal[PartType.VIDEO] = PartType.VIDEO
    src: str = ""
    mime: str = "video/mp4"
    duration_ms: int = 0

Part = Annotated[Union[TextPart, ImagePart, FilePart, AudioPart, VideoPart],
                 Field(discriminator="type")]
```

## 工具类型 — `core/schema/tool.py`

```python
class ToolCallFunction(BaseModel):
    name: str = ""
    arguments: str = ""                    # JSON 字符串

class ToolCall(BaseModel):
    id: ToolCallId
    type: Literal["function"] = "function"
    function: ToolCallFunction = Field(default_factory=ToolCallFunction)
```

## 消息类型 — `core/schema/message.py`

按 role 拆分，Pydantic BaseModel。`role` 字段 `frozen`，构造时自动填值。

```python
from enum import StrEnum, auto

class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"

class UserMessage(BaseModel):
    role: MessageRole = Field(default=MessageRole.USER, frozen=True)
    content: list[Part]
    message_id: MessageId
    timestamp: IsoTimestamp = Field(default_factory=now_iso)
    session_id: SessionId = SessionId("")
    metadata: dict = Field(default_factory=dict)

class AssistantMessage(BaseModel):
    role: MessageRole = Field(default=MessageRole.ASSISTANT, frozen=True)
    content: list[Part]
    message_id: MessageId
    timestamp: IsoTimestamp = Field(default_factory=now_iso)
    tool_calls: list[ToolCall] | None = None
    block_index: dict[str, list[int]] | None = None    # {"markdown": [3,7], "code": [1]}
    inner_voice: str | None = None
    token_usage: dict | None = None
    metadata: dict = Field(default_factory=dict)

class ToolResultMessage(BaseModel):
    role: MessageRole = Field(default=MessageRole.TOOL, frozen=True)
    content: list[Part]
    tool_call_id: ToolCallId
    tool_name: str
    success: bool = True
    error: str | None = None
    error_type: ErrorType | None = None    # 复用 Module/tool/protocol.py 的 ErrorType
    duration_ms: float = 0
    truncated: bool = False
    message_id: MessageId = Field(default=MessageId(""))
    timestamp: IsoTimestamp = Field(default_factory=now_iso)

Message = Union[UserMessage, AssistantMessage, ToolResultMessage]
```

## 适配器 — `core/schema/adapters/message_adapter.py`

纯函数，零状态。依赖 `core/schema` 类型和 `Module/tool/protocol` ErrorType：

- `content_to_openai(parts: list[Part]) -> str | list[dict]` — 单条纯文本返回 str，多模态返回 OpenAI 格式 list[dict]
- `content_text(parts: list[Part]) -> str` — 提取所有文本拼接
- `has_image(parts: list[Part]) -> bool`
- `parts_to_openai_multimodal(parts: list[Part]) -> list[dict]`
- 以及从旧 `message_format.py` 迁移来的其他辅助函数（`_resolve_image_source`、`_to_data_uri` 等）

## 兼容性

### Module/schema/message_format.py

变为 re-export 层，从 `core/schema` 导入再导出，保持现有 import 路径不中断。文件头标注 DEPRECATED，提示迁移到 `core/schema`。后续逐步替换调用方后删除。

### ChatMessageV2

旧 `ChatMessageV2` 保留在 `message_format.py` 的 re-export 中。新代码使用 `core/schema.message.UserMessage | AssistantMessage | ToolResultMessage`。

## 测试

- `core/schema/tests/` 目录建测试
- Part: 验证 discriminated union 正确区分 5 种 type，model_validate/model_dump 往返
- Message: 验证 role frozen、timestamp 校验、Union 反序列化
- Adapter: 验证 content_to_openai 纯文本/多模态两种路径

## 不在首期范围内的

- `core/schema.py` 内容暂不改动（枚举 + Event），仅移动位置
- Session params 的 dataclass 化 — 后续
- `CoreEvent` 和 `Module/schema/event.py` 合并 — 后续
- WS 消息格式、MongoDB 文档 schema — 后续
