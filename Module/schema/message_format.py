"""多模态消息格式 — Part 类型 + ChatMessage V2。

对齐 OpenAI multimodal content 格式：
  - 纯文本:  {"type": "text", "text": "..."}
  - 图片:    {"type": "image_url", "image_url": {"url": "..."}}
  - 文件:    {"type": "file", "file": {...}}  (Phase 2)
  - 视频:    {"type": "video", "video": {...}}  (Phase 2)

内部使用 Part dataclass 统一承载，PromptAssembler 负责转换为 OpenAI API 格式。
"""

from __future__ import annotations

import base64 as _base64
import time
from dataclasses import dataclass, field
from pathlib import Path as _Path
from typing import Union

from core.message_id import new_message_id


# ============================================================
# Part 类型 — content 数组中的每个元素
# ============================================================

@dataclass
class TextPart:
    """文本片段。"""
    type: str = "text"
    content: str = ""


@dataclass
class ImagePart:
    """图片 — URL 或 base64 data URI。"""
    type: str = "image"
    source: str = ""          # URL (https://...) 或 base64 data URI (data:image/png;base64,...)
    mime: str = "image/png"   # image/png, image/jpeg, image/gif, image/webp
    detail: str = "auto"      # OpenAI: auto / low / high
    width: int | None = None
    height: int | None = None


@dataclass
class FilePart:
    """文件附件（Phase 2 预留）。"""
    type: str = "file"
    source: str = ""          # URL
    mime: str = "application/octet-stream"
    filename: str = ""
    size_bytes: int = 0


@dataclass
class AudioPart:
    """音频（Phase 2 预留）。"""
    type: str = "audio"
    source: str = ""          # URL
    mime: str = "audio/wav"
    duration_ms: int = 0

@dataclass
class VideoPart:
    """视频（Phase 2 预留）。"""
    type: str = "video"
    source: str = ""          # URL
    mime: str = "video/mp4"
    duration_ms: int = 0

# 联合类型
Part = Union[TextPart, ImagePart, FilePart, AudioPart, VideoPart]


# ============================================================
# Part 工具函数
# ============================================================

_PART_CLASSES: dict[str, type] = {
    "text": TextPart,
    "image": ImagePart,
    "file": FilePart,
    "audio": AudioPart,
    "video": VideoPart
}


def part_from_dict(d: dict) -> Part:
    """从 dict 还原 Part 实例。"""
    t = d.get("type", "text")
    cls = _PART_CLASSES.get(t, TextPart)
    if cls is TextPart:
        return TextPart(type="text", content=d.get("content", ""))
    elif cls is ImagePart:
        return ImagePart(
            type="image",
            source=d.get("source", ""),
            mime=d.get("mime", "image/png"),
            detail=d.get("detail", "auto"),
            width=d.get("width"),
            height=d.get("height"),
        )
    elif cls is FilePart:
        return FilePart(
            type="file",
            source=d.get("source", ""),
            mime=d.get("mime", "application/octet-stream"),
            filename=d.get("filename", ""),
            size_bytes=d.get("size_bytes", 0),
        )
    elif cls is AudioPart:
        return AudioPart(
            type="audio",
            source=d.get("source", ""),
            mime=d.get("mime", "audio/wav"),
            duration_ms=d.get("duration_ms", 0),
        )
    elif cls is VideoPart:
        return VideoPart(
            type="video",
            source=d.get("source", ""),
            mime=d.get("mime", "video/mp4"),
            duration_ms=d.get("duration_ms", 0),
        )
    return TextPart(type="text", content=str(d))


def parts_to_list(parts: list[Part]) -> list[dict]:
    """Part 列表 → dict 列表（用于 to_dict / 序列化）。"""
    result: list[dict] = []
    for p in parts:
        d = {"type": p.type}
        if isinstance(p, TextPart):
            d["content"] = p.content
        elif isinstance(p, ImagePart):
            d["source"] = p.source
            d["mime"] = p.mime
            if p.detail != "auto":
                d["detail"] = p.detail
            if p.width is not None:
                d["width"] = p.width
            if p.height is not None:
                d["height"] = p.height
        elif isinstance(p, FilePart):
            d["source"] = p.source
            d["mime"] = p.mime
            d["filename"] = p.filename
            if p.size_bytes:
                d["size_bytes"] = p.size_bytes
        elif isinstance(p, AudioPart):
            d["source"] = p.source
            d["mime"] = p.mime
            if p.duration_ms:
                d["duration_ms"] = p.duration_ms
        elif isinstance(p, VideoPart):
            d["source"] = p.source
            d["mime"] = p.mime
            if p.duration_ms:
                d["duration_ms"] = p.duration_ms
        result.append(d)
    return result


def parts_from_list(data: list[dict]) -> list[Part]:
    """dict 列表 → Part 列表（从序列化数据还原）。"""
    return [part_from_dict(d) for d in data]


def content_text(parts: list[Part]) -> str:
    """提取所有文本片段拼接（给 STT / 旧接口兼容用）。"""
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


# ============================================================
# OpenAI API 格式转换
# ============================================================

_IMAGES_DIR = _Path(__file__).resolve().parent.parent.parent / "data" / "images"
_STATIC_DIR = _Path(__file__).resolve().parent.parent.parent / "static"


def _resolve_image_source(source: str) -> str:
    """将本地 source 转为 base64 data URI，远程 URL 原样返回。"""
    # 已经是 data URI，直接返回
    if source.startswith("data:"):
        return source
    # 远程 URL，直接返回
    if source.startswith("http://") or source.startswith("https://"):
        # 本地回环地址 → 尝试读取对应文件
        if "localhost" in source or "127.0.0.1" in source:
            # 从 URL 中提取文件名（如 /static/images/abc.png → abc.png）
            filename = source.rsplit("/", 1)[-1]
            for search_dir in (_IMAGES_DIR, _STATIC_DIR):
                local_path = search_dir / filename
                if local_path.exists():
                    return _to_data_uri(local_path)
        return source
    # 本地文件路径
    local_path = _Path(source)
    if local_path.exists():
        return _to_data_uri(local_path)
    return source


def _to_data_uri(path: _Path) -> str:
    """读取文件，返回 data URI。"""
    data = path.read_bytes()
    ext = path.suffix.lower()
    mime_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }
    mime = mime_map.get(ext, "image/png")
    b64 = _base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def content_to_openai(content: list[dict]) -> str | list[dict]:
    """将内部 content 数组转换为 OpenAI API 格式。

    纯文本单条 → 返回 str（兼容纯文本 API）
    含图片/多 part → 返回 list[dict]（OpenAI multimodal 格式）
    """
    # 单条纯文本 → 直接返回 str
    if len(content) == 1 and content[0].get("type") == "text":
        return content[0].get("content", "")

    # 多条或多模态 → 构建 OpenAI multimodal content array
    # MIMO 要求 image 块在 text 块之前
    image_blocks: list[dict] = []
    text_blocks: list[dict] = []
    for p in content:
        t = p.get("type", "text")
        if t == "image":
            source = _resolve_image_source(p.get("source", ""))
            image_blocks.append({
                "type": "image_url",
                "image_url": {
                    "url": source,
                    "detail": p.get("detail", "auto"),
                },
            })
        elif t == "text":
            text_blocks.append({"type": "text", "text": p.get("content", "")})
        # file/audio/video → Phase 2 处理
    return image_blocks + text_blocks


# ============================================================
# ChatMessage V2 — content 统一为 Part 数组
# ============================================================

@dataclass
class ChatMessageV2:
    """系统内通用消息 V2 — content 为 Part 数组，统一承载文本/图片/文件/音频。

    to_dict() 输出 OpenAI 兼容格式：
      {"role": "user", "content": [{"type": "text", "content": "..."}, ...]}
    """

    role: str
    content: list[Part] = field(default_factory=list)
    message_id: str = field(default_factory=lambda: new_message_id())
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""
    reply_to: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_multimodal(self) -> bool:
        return has_image(self.content) or has_file(self.content) or has_video(self.content)

    @property
    def text(self) -> str:
        """提取纯文本内容（给日志/搜索用）。"""
        return content_text(self.content)

    def to_dict(self) -> dict:
        """转为 session 存储格式。content 序列化为 list[dict]。"""
        return {
            "role": self.role,
            "content": parts_to_list(self.content),
            "message_id": self.message_id,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "reply_to": self.reply_to,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ChatMessageV2:
        """从 session dict 还原。兼容旧格式 content: str。"""
        raw = data.get("content", "")
        if isinstance(raw, list):
            parts = parts_from_list(raw)
        elif isinstance(raw, str):
            # 向后兼容：旧消息 content 为纯字符串
            parts = [TextPart(type="text", content=raw)]
        else:
            parts = [TextPart(type="text", content=str(raw))]
        return cls(
            role=data.get("role", ""),
            content=parts,
            message_id=data.get("message_id", new_message_id()),
            timestamp=data.get("timestamp", time.time()),
            session_id=data.get("session_id", ""),
            reply_to=data.get("reply_to"),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_text(cls, role: str, text: str, **kwargs) -> ChatMessageV2:
        """从纯文本创建（便捷方法）。"""
        return cls(
            role=role,
            content=[TextPart(type="text", content=text)],
            **kwargs,
        )
