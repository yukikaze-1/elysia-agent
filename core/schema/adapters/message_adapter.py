# core/schema/adapters/message_adapter.py — 消息格式适配器
"""消息格式适配器 — OpenAI / Legacy 格式转换。

纯函数，零状态。依赖 core/schema 类型。
消息格式转换函数，依赖 core/schema 类型。

边界约定：外部数据（session 存储、WS 请求）是 list[dict]，
用 parts_from_dicts() 在边界处转为 list[Part]，内部逻辑只处理 Part。
"""

import base64 as _base64
import re as _re
from pathlib import Path as _Path
from typing import Annotated, Any, Union

from pydantic import Discriminator, Tag, TypeAdapter, ValidationError

from core.schema.adapters import attachment_resolver as _attachment_resolver
from core.schema.part import Part, TextPart, ImagePart, FilePart, AudioPart, VideoPart, ImageMime
from core.schema.message import Message, AssistantMessage, ToolResultMessage, UserMessage


# ── dict → Part 边界转换 ──────────────────────────────

_PARTS_ADAPTER: TypeAdapter[list[Part]] = TypeAdapter(list[Part])


# ── Message 校验 + 旧 token_usage 降级 ──────────────────

def _discriminate_message_role(v: Any) -> str | None:
    """按 role 判别 Message 联合成员。非 dict 输入返回 None。"""
    if isinstance(v, dict):
        return v.get("role")
    return None


# 带 role 判别标签的 Message 联合：按 role 可靠路由到对应模型，
# 避免 Pydantic 普通 union 把 role=assistant 的 dict 静默解析为 UserMessage
# （token_usage 被当作 UserMessage 的未知额外字段忽略，导致角色错乱）。
_MessageByRole = Annotated[
    Union[
        Annotated[UserMessage, Tag("user")],
        Annotated[AssistantMessage, Tag("assistant")],
        Annotated[ToolResultMessage, Tag("tool")],
    ],
    Discriminator(_discriminate_message_role),
]
_MESSAGE_ADAPTER: TypeAdapter[Message] = TypeAdapter(_MessageByRole)


def validate_message_legacy_compat(data: dict) -> Message:
    """校验消息 dict → Message；旧格式 token_usage 剥除后重试。

    旧会话/归档消息的 token_usage 为 prompt_tokens/completion_tokens 旧字段，
    新模型无法校验。剥除 token_usage（置 None）保留消息正文，避免整批加载
    失败导致会话上下文丢失。token_usage 不存在或重试仍失败时抛原异常。
    """
    try:
        return _MESSAGE_ADAPTER.validate_python(data)
    except ValidationError:
        if isinstance(data, dict) and "token_usage" in data:
            try:
                return _MESSAGE_ADAPTER.validate_python({**data, "token_usage": None})
            except ValidationError:
                pass
        raise  # 重新抛出原始异常


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

# 附件稳定路径 /api/attachments/{attachment_id}（批次 7 消息关联 wire 格式）
_ATTACHMENT_SRC_RE = _re.compile(r"^/api/attachments/([^/]+)$")


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
    # 批次 7：附件稳定路径 → AttachmentFileResolver 定位落盘文件 → data URI
    m = _ATTACHMENT_SRC_RE.match(src)
    if m:
        local_path = _attachment_resolver.resolve(m.group(1))
        if local_path is not None and local_path.is_file():
            return _to_data_uri(local_path)
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
        # FilePart/AudioPart/VideoPart：Phase 2 阶段再处理
    return image_blocks + text_blocks
