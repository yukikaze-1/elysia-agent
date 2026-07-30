# core/schema/adapters/message_adapter.py
"""消息格式适配器 — OpenAI / Legacy 格式转换。

纯函数，零状态。依赖 core/schema 类型。
消息格式转换函数，依赖 core/schema 类型。

边界约定：外部数据（session 存储、WS 请求）是 list[dict]，
用 parts_from_dicts() 在边界处转为 list[Part]，内部逻辑只处理 Part。
"""

import base64 as _base64
from pathlib import Path as _Path

from pydantic import TypeAdapter

from ..part import Part, TextPart, ImagePart, FilePart, AudioPart, VideoPart, ImageMime


# ── dict → Part 边界转换 ──────────────────────────────

_PARTS_ADAPTER: TypeAdapter[list[Part]] = TypeAdapter(list[Part])


def parts_from_dicts(dicts: list[dict]) -> list[Part]:
    """边界转换：外部 list[dict]（session 存储 / WS 请求）→ list[Part]。

    Args:
        dicts: 外部的 Part dict 列表，每个 dict 需包含 type 等字段。

    Returns:
        校验并转换后的 Part 对象列表。

    Raises:
        ValidationError: 无法识别的 type 或字段格式错误。
    """
    return _PARTS_ADAPTER.validate_python(dicts)


# ── 辅助函数 ──────────────────────────────────────────

def content_text(parts: list[Part]) -> str:
    """提取所有文本片段拼接。

    Args:
        parts: Part 对象列表。

    Returns:
        拼接后的纯文本字符串。
    """
    return "".join(p.content for p in parts if isinstance(p, TextPart))


def has_image(parts: list[Part]) -> bool:
    """是否包含图片。

    Args:
        parts: Part 对象列表。

    Returns:
        True 如果列表中包含 ImagePart。
    """
    return any(isinstance(p, ImagePart) for p in parts)


def has_file(parts: list[Part]) -> bool:
    """是否包含文件。

    Args:
        parts: Part 对象列表。

    Returns:
        True 如果列表中包含 FilePart。
    """
    return any(isinstance(p, FilePart) for p in parts)


def has_video(parts: list[Part]) -> bool:
    """是否包含视频。

    Args:
        parts: Part 对象列表。

    Returns:
        True 如果列表中包含 VideoPart。
    """
    return any(isinstance(p, VideoPart) for p in parts)


def has_audio(parts: list[Part]) -> bool:
    """是否包含音频。

    Args:
        parts: Part 对象列表。

    Returns:
        True 如果列表中包含 AudioPart。
    """
    return any(isinstance(p, AudioPart) for p in parts)


# ── OpenAI 格式转换 ────────────────────────────────────

from paths import ProjectPath as _ProjectPath

_IMAGES_DIR = _ProjectPath.DATA_UPLOADS_IMAGES.resolve()
_STATIC_DIR = _ProjectPath.STATIC.resolve()


def _to_data_uri(path: _Path) -> str:
    """读取本地文件，编码为 data URI。

    Args:
        path: 本地图片文件路径。

    Returns:
        base64 编码后的 data URI 字符串。
    """
    data = path.read_bytes()
    ext = path.suffix.lower()
    mime_map = {
        ".png": ImageMime.PNG, ".jpg": ImageMime.JPEG, ".jpeg": ImageMime.JPEG,
        ".gif": ImageMime.GIF, ".webp": ImageMime.WEBP,
    }
    mime = mime_map.get(ext, ImageMime.PNG)
    b64 = _base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _resolve_image_source(src: str) -> str:
    """将本地 src 转为 base64 data URI，远程 URL 原样返回。

    Args:
        src: 图片源路径或 URL。

    Returns:
        如果是本地文件则返回 base64 data URI，否则原样返回。
    """
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

    Args:
        parts: Part 对象列表。

    Returns:
        纯文本单条时返回 str（兼容纯文本 API），含图片或多 part 时返回
        list[dict]（OpenAI multimodal 格式），图片块排在文本块之前。
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
