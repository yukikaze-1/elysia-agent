# core/schema/part.py
"""多模态 Part 类型 — Pydantic BaseModel + discriminated union。

对齐 OpenAI multimodal content 格式：
  - 纯文本: {"type": "text", "text": "..."}
  - 图片:   {"type": "image_url", "image_url": {"url": "..."}}

内部使用 Part discriminated union 统一承载，通过 model_validate/model_dump 序列化。
"""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import AfterValidator, BaseModel, Field


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


class FileMime(StrEnum):
    OCTET_STREAM = "application/octet-stream"
    PDF = "application/pdf"
    PLAIN = "text/plain"
    JSON = "application/json"


class AudioMime(StrEnum):
    WAV = "audio/wav"
    MP3 = "audio/mp3"
    MPEG = "audio/mpeg"
    OGG = "audio/ogg"
    WEBM = "audio/webm"


class VideoMime(StrEnum):
    MP4 = "video/mp4"
    WEBM = "video/webm"
    OGG = "video/ogg"


# ── src 字段校验 ────────────────────────────────────────

def _validate_src(v: str) -> str:
    """校验 Part src 字段：不能为空，必须是合法 URL / data URI / 绝对路径 / 存在的本地文件。"""
    if not v:
        raise ValueError("src 不能为空")
    if v.startswith(("http://", "https://", "data:", "/")):
        return v
    if Path(v).exists():
        return v
    raise ValueError(f"无效的 src: {v!r}")

Src = Annotated[str, AfterValidator(_validate_src)]


# ── Part 子类 ──────────────────────────────────────────

class TextPart(BaseModel):
    """文本片段。"""
    type: Literal[PartType.TEXT] = PartType.TEXT
    content: str = ""


class ImagePart(BaseModel):
    """图片 — URL / data URI / 本地路径。"""
    type: Literal[PartType.IMAGE] = PartType.IMAGE
    src: Src = ""                              # URL / data URI / 本地路径
    mime: ImageMime = ImageMime.PNG
    detail: ImageDetail = ImageDetail.AUTO  # OpenAI 视觉模型分辨率控制
    width: int | None = None
    height: int | None = None


class FilePart(BaseModel):
    """文件附件（Phase 2 预留）。"""
    type: Literal[PartType.FILE] = PartType.FILE
    src: Src = ""
    mime: FileMime = FileMime.OCTET_STREAM
    filename: str = ""
    size_bytes: int = 0


class AudioPart(BaseModel):
    """音频（Phase 2 预留）。"""
    type: Literal[PartType.AUDIO] = PartType.AUDIO
    src: Src = ""
    mime: AudioMime = AudioMime.WAV
    duration_ms: int = 0


class VideoPart(BaseModel):
    """视频（Phase 2 预留）。"""
    type: Literal[PartType.VIDEO] = PartType.VIDEO
    src: Src = ""
    mime: VideoMime = VideoMime.MP4
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
