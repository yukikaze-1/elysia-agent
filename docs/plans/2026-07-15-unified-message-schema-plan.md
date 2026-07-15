# core/schema 统一消息类型 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `core/schema/` 目录包作为系统基础类型定义层，首期迁移消息/Part/工具调用类型，Pydantic BaseModel 替代 dataclass。

**Architecture:** 新建 `core/schema/` 包，按职责拆为 types/part/tool/message/adapters 五个子模块。旧 `core/schema.py` 内容移入 `agent.py`，旧 `Module/schema/message_format.py` 变为兼容 re-export。所有新类型使用 Pydantic BaseModel + discriminated union。

**Tech Stack:** Python 3.13, Pydantic v2, Pytest

## Global Constraints

- 遵循 `docs/readme-conventions.md`：新模块必须创建 README（面包屑 + 相关文档 + 根 README 注册）
- 所有新测试标记 `@pytest.mark.unit`
- 使用 conda 环境：`conda activate Elysia`
- `Module/schema/message_format.py` 保留为兼容 re-export，不删除
- `SessionId` 与 `Module/task_system/schema/types.py` 双定义共存（Phase 2 统一迁移）

---

### Task 1: 创建基础类型 — `core/schema/types.py`

**Files:**
- Create: `core/schema/__init__.py`（临时空文件）
- Create: `core/schema/types.py`
- Create: `core/schema/tests/__init__.py`（空文件）
- Create: `core/schema/tests/test_types.py`

**Interfaces:**
- Produces: `IsoTimestamp`, `now_iso()`, `SessionId`, `MessageId`, `ToolCallId`

- [ ] **Step 1: 创建目录结构和 types.py**

```bash
mkdir -p core/schema/tests core/schema/adapters
touch core/schema/__init__.py
touch core/schema/adapters/__init__.py
touch core/schema/tests/__init__.py
```

```python
# core/schema/types.py
"""core/schema 基础类型 — 时间戳、标识符别名。"""

from datetime import datetime, timezone
from typing import Annotated, NewType

from pydantic import BeforeValidator


def _normalize_iso_ts(ts: str) -> str:
    """归一化 ISO 8601 时间戳：补时区、校验格式。"""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def now_iso() -> str:
    """返回当前 UTC 时间 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


IsoTimestamp = Annotated[str, BeforeValidator(_normalize_iso_ts)]
"""ISO 8601 时间戳，构造时自动校验格式并归一化到 UTC。

使用 Pydantic BeforeValidator：空字符串和格式错误会抛出 ValidationError。
"""

SessionId = NewType("SessionId", str)
"""会话唯一标识符。

注意：与 Module/task_system/schema/types.py 的 SessionId 同名共存（Phase 1）。
两者是相同的 NewType，运行时完全兼容（都是 str）。Phase 2 统一迁移到此。
"""

MessageId = NewType("MessageId", str)
"""消息唯一标识符。"""

ToolCallId = NewType("ToolCallId", str)
"""工具调用唯一标识符。"""
```

- [ ] **Step 2: 写 types 测试**

```python
# core/schema/tests/test_types.py
"""core/schema/types.py 测试。"""
import pytest
from datetime import datetime, timezone
from pydantic import BaseModel, ValidationError
from core.schema.types import IsoTimestamp, now_iso, SessionId, MessageId, ToolCallId


@pytest.mark.unit
class TestIsoTimestamp:
    """IsoTimestamp 校验和 now_iso 测试。"""

    def test_valid_iso_passes(self):
        """合法 ISO 8601 通过校验。"""

        class M(BaseModel):
            ts: IsoTimestamp

        m = M(ts="2026-07-15T12:00:00+00:00")
        assert "2026-07-15T12:00:00+00:00" in m.ts

    def test_missing_tz_gets_utc(self):
        """缺时区自动补 UTC。"""

        class M(BaseModel):
            ts: IsoTimestamp

        m = M(ts="2026-07-15T12:00:00")
        assert m.ts.endswith("+00:00")

    def test_empty_string_rejected(self):
        """空字符串被拒绝。"""

        class M(BaseModel):
            ts: IsoTimestamp

        with pytest.raises(ValidationError):
            M(ts="")

    def test_now_iso_returns_valid(self):
        """now_iso() 返回合法 ISO 8601。"""
        ts = now_iso()
        assert isinstance(ts, str)
        assert "T" in ts
        # 应该能 round-trip
        datetime.fromisoformat(ts)


@pytest.mark.unit
class TestNewTypes:
    """NewType 别名行为测试。"""

    def test_session_id_is_str(self):
        """SessionId 是 str 的子类型。"""
        sid = SessionId("test-session")
        assert isinstance(sid, str)
        assert sid == "test-session"

    def test_message_id_is_str(self):
        mid = MessageId("msg-001")
        assert isinstance(mid, str)

    def test_tool_call_id_is_str(self):
        tcid = ToolCallId("call-001")
        assert isinstance(tcid, str)
```

- [ ] **Step 3: 运行测试**

```bash
python -m pytest core/schema/tests/test_types.py -v --tb=short
```

Expected: 4 PASS（TestIsoTimestamp）+ 3 PASS（TestNewTypes）= 7 passed

- [ ] **Step 4: 提交**

```bash
git add core/schema/__init__.py core/schema/types.py core/schema/tests/__init__.py core/schema/tests/test_types.py
git commit -m "feat: core/schema/types.py — IsoTimestamp + NewType 别名"
```

---

### Task 2: 创建 Part 类型 — `core/schema/part.py`

**Files:**
- Create: `core/schema/part.py`
- Create: `core/schema/tests/test_part.py`

**Interfaces:**
- Consumes: `core.schema.types` (无直接依赖)
- Produces: `PartType`, `ImageMime`, `ImageDetail`, `TextPart`, `ImagePart`, `FilePart`, `AudioPart`, `VideoPart`, `Part` (discriminated union)

- [ ] **Step 1: 创建 part.py**

```python
# core/schema/part.py
"""多模态 Part 类型 — Pydantic BaseModel + discriminated union。

对齐 OpenAI multimodal content 格式：
  - 纯文本: {"type": "text", "text": "..."}
  - 图片:   {"type": "image_url", "image_url": {"url": "..."}}

内部使用 Part discriminated union 统一承载，通过 model_validate/model_dump 序列化。
"""

from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


# ── 枚举 ──────────────────────────────────────────────

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
    """OpenAI vision 分辨率控制。"""
    AUTO = "auto"
    LOW = "low"
    HIGH = "high"


# ── Part 子类 ──────────────────────────────────────────

class TextPart(BaseModel):
    """文本片段。"""
    type: Literal[PartType.TEXT] = PartType.TEXT
    content: str = ""


class ImagePart(BaseModel):
    """图片 — URL / data URI / 本地路径。"""
    type: Literal[PartType.IMAGE] = PartType.IMAGE
    src: str = ""                           # URL / data URI / 本地路径
    mime: ImageMime = ImageMime.PNG
    detail: ImageDetail = ImageDetail.AUTO  # OpenAI 视觉模型分辨率控制
    width: int | None = None
    height: int | None = None


class FilePart(BaseModel):
    """文件附件（Phase 2 预留）。"""
    type: Literal[PartType.FILE] = PartType.FILE
    src: str = ""
    mime: str = "application/octet-stream"
    filename: str = ""
    size_bytes: int = 0


class AudioPart(BaseModel):
    """音频（Phase 2 预留）。"""
    type: Literal[PartType.AUDIO] = PartType.AUDIO
    src: str = ""
    mime: str = "audio/wav"
    duration_ms: int = 0


class VideoPart(BaseModel):
    """视频（Phase 2 预留）。"""
    type: Literal[PartType.VIDEO] = PartType.VIDEO
    src: str = ""
    mime: str = "video/mp4"
    duration_ms: int = 0


# ── Discriminated Union ────────────────────────────────

Part = Annotated[
    Union[TextPart, ImagePart, FilePart, AudioPart, VideoPart],
    Field(discriminator="type"),
]
"""多模态 Part 联合类型。根据 type 字段自动区分具体 Part 子类。

用法：
    # 构造
    text = TextPart(content="你好")
    img = ImagePart(src="https://example.com/img.png")

    # 序列化（Pydantic model_dump）
    text.model_dump()  # {"type": "text", "content": "你好"}

    # 反序列化（从 dict 还原）
    TextPart.model_validate({"type": "text", "content": "hello"})
"""
```

- [ ] **Step 2: 写 Part 测试**

```python
# core/schema/tests/test_part.py
"""core/schema/part.py 测试。"""
import pytest
from pydantic import ValidationError
from core.schema.part import (
    PartType, ImageMime, ImageDetail,
    TextPart, ImagePart, FilePart, AudioPart, VideoPart,
)


@pytest.mark.unit
class TestPartConstruction:
    """Part 构造和序列化。"""

    def test_text_part_defaults(self):
        p = TextPart(content="hello")
        assert p.type == PartType.TEXT
        assert p.content == "hello"
        d = p.model_dump()
        assert d == {"type": "text", "content": "hello"}

    def test_text_part_validate(self):
        p = TextPart.model_validate({"type": "text", "content": "world"})
        assert p.content == "world"

    def test_image_part_defaults(self):
        p = ImagePart(src="https://example.com/img.png")
        assert p.type == PartType.IMAGE
        assert p.src == "https://example.com/img.png"
        assert p.mime == ImageMime.PNG
        assert p.detail == ImageDetail.AUTO

    def test_image_part_validate(self):
        p = ImagePart.model_validate({
            "type": "image",
            "src": "https://example.com/img.jpg",
            "mime": "image/jpeg",
            "width": 800,
            "height": 600,
        })
        assert p.mime == ImageMime.JPEG
        assert p.width == 800
        assert p.height == 600

    def test_image_part_detail_enum(self):
        p = ImagePart(detail=ImageDetail.HIGH)
        assert p.detail == ImageDetail.HIGH
        d = p.model_dump()
        assert d["detail"] == "high"

    def test_file_part_defaults(self):
        p = FilePart()
        assert p.type == PartType.FILE
        assert p.mime == "application/octet-stream"

    def test_audio_part_defaults(self):
        p = AudioPart()
        assert p.type == PartType.AUDIO

    def test_video_part_defaults(self):
        p = VideoPart()
        assert p.type == PartType.VIDEO

    def test_wrong_type_rejected(self):
        """type 字段错误时抛 ValidationError。"""
        with pytest.raises(ValidationError):
            TextPart.model_validate({"type": "image", "src": "x"})

    def test_roundtrip_text(self):
        """model_dump → model_validate 往返。"""
        p = TextPart(content="roundtrip test")
        p2 = TextPart.model_validate(p.model_dump())
        assert p2.content == p.content
```

- [ ] **Step 3: 运行测试**

```bash
python -m pytest core/schema/tests/test_part.py -v --tb=short
```

Expected: 10 passed

- [ ] **Step 4: 提交**

```bash
git add core/schema/part.py core/schema/tests/test_part.py
git commit -m "feat: core/schema/part.py — Part 类型 Pydantic + discriminated union"
```

---

### Task 3: 创建工具类型 — `core/schema/tool.py`

**Files:**
- Create: `core/schema/tool.py`
- Create: `core/schema/tests/test_tool.py`

**Interfaces:**
- Consumes: `core.schema.types.ToolCallId`
- Produces: `ToolCallFunction`, `ToolCall`

- [ ] **Step 1: 创建 tool.py**

```python
# core/schema/tool.py
"""工具调用类型 — OpenAI 兼容 tool_calls 结构。"""

from typing import Literal

from pydantic import BaseModel, Field

from .types import ToolCallId


class ToolCallFunction(BaseModel):
    """工具调用的 function 块。"""
    name: str = ""
    arguments: str = ""  # JSON 字符串


class ToolCall(BaseModel):
    """单个工具调用 — OpenAI compatible。

    对应 OpenAI API 响应中的 tool_calls[] 元素：
      {"id": "call_xxx", "type": "function", "function": {"name": "...", "arguments": "..."}}
    """
    id: ToolCallId
    type: Literal["function"] = "function"
    function: ToolCallFunction = Field(default_factory=ToolCallFunction)
```

- [ ] **Step 2: 写 tool 测试**

```python
# core/schema/tests/test_tool.py
"""core/schema/tool.py 测试。"""
import pytest
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId


@pytest.mark.unit
class TestToolCall:
    """ToolCall 构造和序列化。"""

    def test_minimal_construction(self):
        tc = ToolCall(id=ToolCallId("call_001"))
        assert tc.id == "call_001"
        assert tc.type == "function"
        assert tc.function.name == ""
        assert tc.function.arguments == ""

    def test_full_construction(self):
        tc = ToolCall(
            id=ToolCallId("call_002"),
            function=ToolCallFunction(name="web_search", arguments='{"query":"test"}'),
        )
        assert tc.function.name == "web_search"
        assert tc.function.arguments == '{"query":"test"}'

    def test_model_dump(self):
        tc = ToolCall(
            id=ToolCallId("call_003"),
            function=ToolCallFunction(name="read_file", arguments='{"path":"/tmp/x"}'),
        )
        d = tc.model_dump()
        assert d["id"] == "call_003"
        assert d["type"] == "function"
        assert d["function"]["name"] == "read_file"
        assert d["function"]["arguments"] == '{"path":"/tmp/x"}'

    def test_model_validate_from_openai_format(self):
        """从 OpenAI API 格式的 dict 反序列化。"""
        d = {
            "id": "call_004",
            "type": "function",
            "function": {"name": "get_time", "arguments": '{"timezone":"UTC"}'},
        }
        tc = ToolCall.model_validate(d)
        assert tc.id == "call_004"
        assert tc.function.name == "get_time"
```

- [ ] **Step 3: 运行测试**

```bash
python -m pytest core/schema/tests/test_tool.py -v --tb=short
```

Expected: 4 passed

- [ ] **Step 4: 提交**

```bash
git add core/schema/tool.py core/schema/tests/test_tool.py
git commit -m "feat: core/schema/tool.py — ToolCall + ToolCallFunction 类型"
```

---

### Task 4: 创建消息类型 — `core/schema/message.py`

**Files:**
- Create: `core/schema/message.py`
- Create: `core/schema/tests/test_message.py`

**Interfaces:**
- Consumes: `core.schema.types.IsoTimestamp`, `now_iso`, `SessionId`, `MessageId`, `core.schema.part.Part`, `core.schema.tool.ToolCall`, `ToolCallId`, `Module.tool.protocol.ErrorType`
- Produces: `MessageRole`, `UserMessage`, `AssistantMessage`, `ToolResultMessage`, `Message`

- [ ] **Step 1: 创建 message.py**

```python
# core/schema/message.py
"""消息类型 — 按 role 拆分的 Pydantic BaseModel。

UserMessage / AssistantMessage / ToolResultMessage 各管各的字段。
role 字段 frozen，构造时自动填值。
"""

from enum import StrEnum
from typing import Union

from pydantic import BaseModel, Field

from Module.tool.protocol import ErrorType

from .part import Part
from .tool import ToolCall, ToolCallId
from .types import IsoTimestamp, now_iso, MessageId, SessionId


class MessageRole(StrEnum):
    """消息角色枚举。"""
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class UserMessage(BaseModel):
    """用户消息。"""
    role: MessageRole = Field(default=MessageRole.USER, frozen=True)
    content: list[Part]
    message_id: MessageId
    timestamp: IsoTimestamp = Field(default_factory=now_iso)
    session_id: SessionId = SessionId("")
    metadata: dict = Field(default_factory=dict)


class AssistantMessage(BaseModel):
    """Assistant 回复消息。"""
    role: MessageRole = Field(default=MessageRole.ASSISTANT, frozen=True)
    content: list[Part]
    message_id: MessageId
    timestamp: IsoTimestamp = Field(default_factory=now_iso)
    tool_calls: list[ToolCall] | None = None
    block_index: dict[str, list[int]] | None = None  # {"markdown": [3,7], "code": [1]}
    inner_voice: str | None = None
    token_usage: dict | None = None
    metadata: dict = Field(default_factory=dict)


class ToolResultMessage(BaseModel):
    """工具执行结果消息。"""
    role: MessageRole = Field(default=MessageRole.TOOL, frozen=True)
    content: list[Part]
    tool_call_id: ToolCallId
    tool_name: str
    success: bool = True
    error: str | None = None
    error_type: ErrorType | None = None  # 复用 Module/tool/protocol.py 的 ErrorType
    duration_ms: float = 0
    truncated: bool = False
    message_id: MessageId = Field(default=MessageId(""))
    timestamp: IsoTimestamp = Field(default_factory=now_iso)


Message = Union[UserMessage, AssistantMessage, ToolResultMessage]
"""消息联合类型。"""
```

- [ ] **Step 2: 写 message 测试**

```python
# core/schema/tests/test_message.py
"""core/schema/message.py 测试。"""
import pytest
from pydantic import ValidationError
from core.schema.message import MessageRole, UserMessage, AssistantMessage, ToolResultMessage
from core.schema.part import TextPart
from core.schema.types import MessageId, SessionId
from Module.tool.protocol import ErrorType


@pytest.mark.unit
class TestMessageRole:
    """MessageRole 枚举。"""

    def test_roles(self):
        assert MessageRole.USER == "user"
        assert MessageRole.ASSISTANT == "assistant"
        assert MessageRole.TOOL == "tool"
        assert MessageRole.SYSTEM == "system"


@pytest.mark.unit
class TestUserMessage:
    """UserMessage 构造和校验。"""

    def test_minimal_construction(self):
        msg = UserMessage(
            content=[TextPart(content="hello")],
            message_id=MessageId("msg-001"),
        )
        assert msg.role == MessageRole.USER
        assert len(msg.content) == 1
        assert msg.content[0].content == "hello"
        assert msg.timestamp  # now_iso 自动生成

    def test_role_is_frozen(self):
        msg = UserMessage(
            content=[TextPart(content="test")],
            message_id=MessageId("msg-002"),
        )
        with pytest.raises(ValidationError):
            msg.role = MessageRole.ASSISTANT  # frozen

    def test_model_validate_from_dict(self):
        d = {
            "content": [{"type": "text", "content": "hello world"}],
            "message_id": "msg-003",
            "timestamp": "2026-07-15T12:00:00+00:00",
        }
        msg = UserMessage.model_validate(d)
        assert msg.role == MessageRole.USER  # auto-filled
        assert msg.content[0].content == "hello world"
        assert isinstance(msg.message_id, str)
        assert msg.message_id == "msg-003"


@pytest.mark.unit
class TestAssistantMessage:
    """AssistantMessage 构造和校验。"""

    def test_minimal_construction(self):
        msg = AssistantMessage(
            content=[TextPart(content="你好！")],
            message_id=MessageId("msg-004"),
        )
        assert msg.role == MessageRole.ASSISTANT
        assert msg.tool_calls is None
        assert msg.inner_voice is None

    def test_with_tool_calls(self):
        """带 tool_calls 的 assistant 消息。"""
        from core.schema.tool import ToolCall, ToolCallFunction
        from core.schema.types import ToolCallId

        msg = AssistantMessage(
            content=[],
            message_id=MessageId("msg-005"),
            tool_calls=[
                ToolCall(
                    id=ToolCallId("call_001"),
                    function=ToolCallFunction(name="web_search", arguments='{"q":"test"}'),
                )
            ],
        )
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].function.name == "web_search"

    def test_block_index(self):
        msg = AssistantMessage(
            content=[TextPart(content="x")],
            message_id=MessageId("msg-006"),
            block_index={"markdown": [0], "code": []},
        )
        assert msg.block_index == {"markdown": [0], "code": []}


@pytest.mark.unit
class TestToolResultMessage:
    """ToolResultMessage 构造和校验。"""

    def test_success_result(self):
        msg = ToolResultMessage(
            content=[TextPart(content="done")],
            tool_call_id="call_001",
            tool_name="web_search",
            success=True,
            duration_ms=1234.5,
        )
        assert msg.role == MessageRole.TOOL
        assert msg.success is True
        assert msg.error is None
        assert msg.error_type is None
        assert msg.duration_ms == 1234.5

    def test_error_result(self):
        msg = ToolResultMessage(
            content=[TextPart(content="timeout")],
            tool_call_id="call_002",
            tool_name="web_search",
            success=False,
            error="request timed out",
            error_type=ErrorType.TIMEOUT,
        )
        assert msg.success is False
        assert msg.error == "request timed out"
        assert msg.error_type == ErrorType.TIMEOUT
```

- [ ] **Step 3: 运行测试**

```bash
python -m pytest core/schema/tests/test_message.py -v --tb=short
```

Expected: ~10 passed（1 MessageRole + 3 UserMessage + 3 AssistantMessage + 2 ToolResultMessage）

- [ ] **Step 4: 提交**

```bash
git add core/schema/message.py core/schema/tests/test_message.py
git commit -m "feat: core/schema/message.py — 按 role 拆分的消息类型"
```

---

### Task 5: 创建兼容 re-export + 适配器

**Files:**
- Modify: `Module/schema/message_format.py`（变为 re-export）
- Create: `core/schema/adapters/__init__.py`（空文件）
- Create: `core/schema/adapters/message_adapter.py`
- Create: `core/schema/tests/test_adapters.py`

**Interfaces:**
- Consumes: `core.schema.part.*`, `core.schema.message.*`, `core.schema.tool.*`
- Produces: `content_to_openai`, `content_text`, `has_image`, `has_file`, `has_video`, `_resolve_image_source`

- [ ] **Step 1: 创建适配器**

```python
# core/schema/adapters/message_adapter.py
"""消息格式适配器 — OpenAI / Legacy 格式转换。

纯函数，零状态。依赖 core/schema 类型。
从旧 Module/schema/message_format.py 迁移而来。
"""

import base64 as _base64
from pathlib import Path as _Path

from ..part import Part, TextPart, ImagePart, FilePart, AudioPart, VideoPart


# ── 辅助函数 ──────────────────────────────────────────

def content_text(parts: list[Part]) -> str:
    """提取所有文本片段拼接。"""
    return "".join(p.content for p in parts if isinstance(p, TextPart))


def has_image(parts: list[Part]) -> bool:
    """是否包含图片。"""
    return any(isinstance(p, ImagePart) for p in parts)


def has_file(parts: list[Part]) -> bool:
    """是否包含文件。"""
    return any(isinstance(p, FilePart) for p in parts)


def has_video(parts: list[Part]) -> bool:
    """是否包含视频。"""
    return any(isinstance(p, VideoPart) for p in parts)


def has_audio(parts: list[Part]) -> bool:
    """是否包含音频。"""
    return any(isinstance(p, AudioPart) for p in parts)


# ── OpenAI 格式转换 ────────────────────────────────────

_IMAGES_DIR = _Path(__file__).resolve().parent.parent.parent.parent / "data" / "uploads" / "images"
_STATIC_DIR = _Path(__file__).resolve().parent.parent.parent.parent / "static"


def _to_data_uri(path: _Path) -> str:
    """读取本地文件，返回 data URI。"""
    data = path.read_bytes()
    ext = path.suffix.lower()
    mime_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }
    mime = mime_map.get(ext, "image/png")
    b64 = _base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _resolve_image_source(src: str) -> str:
    """将本地 src 转为 base64 data URI，远程 URL 原样返回。"""
    if src.startswith("data:"):
        return src
    if src.startswith("http://") or src.startswith("https://"):
        if "://localhost" in src or "://127.0.0.1" in src:
            filename = src.rsplit("/", 1)[-1]
            for search_dir in (_IMAGES_DIR, _STATIC_DIR):
                local_path = search_dir / filename
                if local_path.exists():
                    return _to_data_uri(local_path)
        return src
    if src.startswith("/"):
        filename = src.rsplit("/", 1)[-1]
        if filename:
            for search_dir in (_IMAGES_DIR, _STATIC_DIR):
                local_path = search_dir / filename
                if local_path.exists():
                    return _to_data_uri(local_path)
        return src
    local_path = _Path(src)
    if local_path.exists():
        return _to_data_uri(local_path)
    return src


def content_to_openai(parts: list[Part]) -> str | list[dict]:
    """将 Part 列表转换为 OpenAI API 格式。

    纯文本单条 → 返回 str（兼容纯文本 API）
    含图片/多 part → 返回 list[dict]（OpenAI multimodal 格式）
    """
    if len(parts) == 1 and isinstance(parts[0], TextPart):
        return parts[0].content

    # MIMO 要求 image 块在 text 块之前
    image_blocks: list[dict] = []
    text_blocks: list[dict] = []
    for p in parts:
        if isinstance(p, ImagePart):
            source = _resolve_image_source(p.src)
            image_blocks.append({
                "type": "image_url",
                "image_url": {
                    "url": source,
                    "detail": p.detail.value if p.detail else "auto",
                },
            })
        elif isinstance(p, TextPart):
            text_blocks.append({"type": "text", "text": p.content})
        # FilePart/AudioPart/VideoPart: Phase 2
    return image_blocks + text_blocks
```

- [ ] **Step 2: 写适配器测试**

```python
# core/schema/tests/test_adapters.py
"""core/schema/adapters/message_adapter.py 测试。"""
import pytest
from core.schema.part import TextPart, ImagePart, FilePart, AudioPart, VideoPart
from core.schema.adapters.message_adapter import (
    content_text, has_image, has_file, has_video, has_audio,
    content_to_openai,
)


@pytest.mark.unit
class TestContentText:
    def test_single_text(self):
        parts = [TextPart(content="hello")]
        assert content_text(parts) == "hello"

    def test_multiple_text(self):
        parts = [TextPart(content="a"), TextPart(content="b")]
        assert content_text(parts) == "ab"

    def test_mixed_parts(self):
        parts = [TextPart(content="text"), ImagePart(src="https://x.com/i.png")]
        assert content_text(parts) == "text"

    def test_empty(self):
        assert content_text([]) == ""


@pytest.mark.unit
class TestHasFunctions:
    def test_has_image_true(self):
        assert has_image([ImagePart(src="https://x.com/i.png")]) is True

    def test_has_image_false(self):
        assert has_image([TextPart(content="hello")]) is False

    def test_has_file(self):
        assert has_file([FilePart()]) is True
        assert has_file([TextPart()]) is False

    def test_has_video(self):
        assert has_video([VideoPart()]) is True

    def test_has_audio(self):
        assert has_audio([AudioPart()]) is True


@pytest.mark.unit
class TestContentToOpenai:
    def test_single_text_returns_str(self):
        result = content_to_openai([TextPart(content="hello")])
        assert result == "hello"
        assert isinstance(result, str)

    def test_multiple_text_returns_list(self):
        result = content_to_openai([TextPart(content="a"), TextPart(content="b")])
        assert isinstance(result, list)
        assert result == [
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
        ]

    def test_image_returns_multimodal(self):
        result = content_to_openai([ImagePart(src="https://example.com/img.png")])
        assert isinstance(result, list)
        assert result[0]["type"] == "image_url"
        assert result[0]["image_url"]["url"] == "https://example.com/img.png"

    def test_empty_parts(self):
        result = content_to_openai([])
        assert result == []
```

- [ ] **Step 3: 运行测试**

```bash
python -m pytest core/schema/tests/test_adapters.py -v --tb=short
```

Expected: ~10 passed

- [ ] **Step 4: 改造 Module/schema/message_format.py 为 re-export**

将 `Module/schema/message_format.py` **整个文件替换**为兼容 re-export：

```python
"""
DEPRECATED: 本模块的类型已迁移至 core/schema/。

请从新位置导入:
  from core.schema.part import TextPart, ImagePart, ...  Part
  from core.schema.message import UserMessage, AssistantMessage, ToolResultMessage

本文件保留为兼容性 re-export，后续逐步删除。
"""
import warnings

warnings.warn(
    "Module.schema.message_format is deprecated. "
    "Use core.schema.part / core.schema.message instead.",
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
    content_to_openai, content_text, has_image, has_file, has_video, has_audio,
    _resolve_image_source, _to_data_uri,
)
from core.schema.types import IsoTimestamp, now_iso  # noqa: F401

# 兼容旧 ChatMessageV2 → 映射为 Message Union
ChatMessageV2 = Message  # noqa: F401
```

> **注意：** `_resolve_image_source` 和 `_to_data_uri` 本来以下划线开头（私有约定），但 `core/tools/vision/plugin.py:15` 直接 import 了 `_resolve_image_source`。兼容 re-export 继续暴露它。

- [ ] **Step 5: 验证兼容性 — 现有代码仍能 import**

```bash
python -c "from Module.schema.message_format import TextPart, ChatMessageV2, content_to_openai, content_text, _resolve_image_source; print('OK')"
```

Expected: `OK`（可能附带一条 DeprecationWarning）

- [ ] **Step 6: 运行全量快速测试确保无回归**

```bash
python -m pytest -m "unit or service" -q -x --tb=short 2>&1 | tail -5
```

- [ ] **Step 7: 提交**

```bash
git add core/schema/adapters/__init__.py core/schema/adapters/message_adapter.py \
        core/schema/tests/test_adapters.py \
        Module/schema/message_format.py
git commit -m "feat: core/schema/adapters + Module/schema/message_format re-export 兼容"
```

---

### Task 6: 迁移 core/schema.py → core/schema/agent.py + 建立 __init__.py

**Files:**
- Create: `core/schema/agent.py`（内容 = 旧 core/schema.py）
- Modify: `core/schema/__init__.py`（统一 re-export）
- Delete: `core/schema.py`（替换为兼容 re-export）
- Create: `core/schema/README.md`

**Interfaces:**
- Consumes: 所有 core/schema 子模块
- Produces: `core.schema` 统一入口

- [ ] **Step 1: 移动 core/schema.py → core/schema/agent.py**

```bash
cp core/schema.py core/schema/agent.py
```

在 `core/schema/agent.py` 顶部加一行 docstring：

```python
"""core/schema 子模块 — Agent 状态、事件、优先级类型。"""
```

- [ ] **Step 2: 更新 core/schema/__init__.py**

```python
# core/schema/ — 系统基础类型定义层
#
# 子模块:
#   agent    — AgentState, EventType, EventPriority, VALID_TRANSITIONS
#   types    — IsoTimestamp, SessionId, MessageId, ToolCallId
#   part     — TextPart, ImagePart, ..., Part
#   tool     — ToolCall, ToolCallFunction
#   message  — UserMessage, AssistantMessage, ToolResultMessage, Message
#   adapters — content_to_openai, content_text, ...

from core.schema.agent import (  # noqa: F401
    AgentState,
    EventPriority,
    EventType,
    VALID_TRANSITIONS,
)
from core.schema.types import (  # noqa: F401
    IsoTimestamp,
    now_iso,
    SessionId,
    MessageId,
    ToolCallId,
)
from core.schema.part import (  # noqa: F401
    PartType,
    ImageMime,
    ImageDetail,
    TextPart,
    ImagePart,
    FilePart,
    AudioPart,
    VideoPart,
    Part,
)
from core.schema.tool import ToolCall, ToolCallFunction  # noqa: F401
from core.schema.message import (  # noqa: F401
    MessageRole,
    UserMessage,
    AssistantMessage,
    ToolResultMessage,
    Message,
)
```

- [ ] **Step 3: 删除 core/schema.py**

`core/schema/__init__.py` 已从 `core/schema/agent.py` re-export 所有类型。Python 包会 shadow 同名模块文件——`core/schema.py` 与 `core/schema/` 目录包共存时，`import core.schema` 永远加载包 `__init__.py`，模块文件成为死代码。直接删除。

```bash
rm core/schema.py
```

- [ ] **Step 4: 验证兼容性**

```bash
python -c "from core.schema import AgentState, EventPriority, EventType, VALID_TRANSITIONS; print('OK')"
```

Expected: `OK`（可能附带 DeprecationWarning）

- [ ] **Step 5: 更新所有对旧 `core.schema` 的 import**

```bash
grep -rn "from core.schema import\|from core import schema\|import core.schema" /home/yomu/Agent/core/ /home/yomu/Agent/Module/ --include="*.py" | grep -v __pycache__ | grep -v ".pyc" | grep -v "/tests/" | grep -v "core/schema/"
```

将找到的文件中的 `from core.schema import ...` 替换为 `from core.schema import ...`（路径不变，因为兼容 re-export 已处理），暂时不强制迁移。后续逐步更新。

- [ ] **Step 6: 提交**

```bash
git add core/schema/agent.py core/schema/__init__.py
git commit -m "refactor: core/schema.py → core/schema/agent.py，删除原文件"
```

---

### Task 7: 创建 core/schema/README.md + 注册到根 README

**Files:**
- Create: `core/schema/README.md`
- Modify: `README.md`（文档导航新增 core/schema 条目）

- [ ] **Step 1: 创建 core/schema/README.md**

```markdown
> 文档导航：[项目文档](../../README.md)

# core/schema — 系统基础类型定义

项目基础数据模型层。当前包含：消息类型、多媒体 Part、工具调用、Agent 状态枚举。

## 模块速查

| 文件 | 内容 |
|------|------|
| `agent.py` | AgentState / EventType / EventPriority / Event / VALID_TRANSITIONS |
| `types.py` | IsoTimestamp / SessionId / MessageId / ToolCallId |
| `part.py` | TextPart / ImagePart / FilePart / AudioPart / VideoPart + Part discriminated union |
| `tool.py` | ToolCall / ToolCallFunction（OpenAI 兼容格式） |
| `message.py` | UserMessage / AssistantMessage / ToolResultMessage / MessageRole |
| `adapters/` | content_to_openai / content_text / has_image 等格式转换函数 |

## 设计原则

- **Pydantic BaseModel**：所有类型使用 Pydantic，提供内置校验和序列化
- **按 role 拆分消息**：替代旧 ChatMessageV2 单类模式
- **Discriminated union**：Part 类型根据 `type` 字段自动区分
- **兼容迁移**：旧 `core/schema.py` 和 `Module/schema/message_format.py` 保留为兼容 re-export

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 文档导航 |
| [数据结构普查](../../docs/data-structure-survey.md) | 全项目 120+ 类型全景 |
| [统一消息 schema 设计](../../docs/specs/2026-07-15-unified-message-schema-design.md) | 本模块的设计 spec |
| [docs/architecture.md](../../docs/architecture.md) | 系统架构 |
```

- [ ] **Step 2: 注册到根 README**

在 `README.md` 的文档导航表中新增一行（放在 Core 总览之后）：

```markdown
| Schema | [core/schema/README.md](core/schema/README.md) | 系统基础类型定义（消息/Part/工具调用） |
```

- [ ] **Step 3: 提交**

```bash
git add core/schema/README.md README.md
git commit -m "docs: core/schema/README.md + 注册到根文档导航"
```

---

### Task 8: 全量验证

- [ ] **Step 1: 运行 unit + service 测试**

```bash
python -m pytest -m "unit or service" -q --tb=short 2>&1 | tail -10
```

- [ ] **Step 2: 检查无 import 错误**

```bash
python -c "
from core.schema import AgentState, TextPart, ImagePart, Part
from core.schema import UserMessage, AssistantMessage, ToolResultMessage, Message
from core.schema import ToolCall, ToolCallFunction
from core.schema import IsoTimestamp, SessionId, MessageId
from core.schema.adapters.message_adapter import content_to_openai, content_text
from Module.schema.message_format import TextPart, content_text
print('All imports OK')
"
```

Expected: `All imports OK`

- [ ] **Step 3: 提交最终变更**

```bash
git status
# 如有未提交变更
git add -A && git commit -m "chore: 全量验证 core/schema 迁移"
```
