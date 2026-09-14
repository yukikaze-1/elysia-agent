# core/schema/adapters/message_adapter.py — 消息格式适配器
"""消息格式适配器 — OpenAI / Legacy 格式转换。

纯函数，零状态。依赖 core/schema 类型。
消息格式转换函数，依赖 core/schema 类型。

边界约定：外部数据（session 存储、WS 请求）是 list[dict]，
用 parts_from_dicts() 在边界处转为 list[Part]，内部逻辑只处理 Part。
"""

import logging
import re as _re
from typing import Annotated, Any, Union

from pydantic import Discriminator, Tag, TypeAdapter, ValidationError

from core.schema.adapters import attachment_resolver as _attachment_resolver
from core.schema.part import Part, TextPart, ImagePart, FilePart, AudioPart, VideoPart, ImageMime
from core.schema.message import Message, AssistantMessage, ToolResultMessage, UserMessage
from core.schema.utils import stable_tool_message_id

logger = logging.getLogger(__name__)


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
    # D1/D2（spec）：存量工具消息 message_id 空/缺失 → 用文档自带 tool_call_id+timestamp
    # 派生稳定 ID（读侧兼容，与回填一致）。tool_call_id/timestamp 缺任一 → 不派生，
    # 留空交 strict 校验失败（畸形文档，绝不 now_iso 兜底）。
    if (isinstance(data, dict) and data.get("role") == "tool"
            and not data.get("message_id")
            and bool(data.get("tool_call_id")) and bool(data.get("timestamp"))):
        data = {**data, "message_id": stable_tool_message_id(
            data["tool_call_id"], data["timestamp"])}
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

# 图片源解析已提取为独立模块（批次 A「配套 API 收口」；理由见该模块 docstring）
from core.schema.adapters.image_source import resolve_image_source


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
            source = resolve_image_source(p.src)
            if not source:
                # S2-H10 的真实残留：`src` 有默认值 `""`，而 pydantic **不校验默认值**
                # （显式传空串会被 `_validate_src` 拦下，legacy 存量数据不会）。空 src
                # 会产出 `{"url": ""}` —— provider 判为非法请求，而 legacy 会话里的坏
                # Part **每一轮**都会重建进 prompt，于是一个坏记录就能让该会话永久
                # 无法对话。**跳过**它（而不是把坏块发出去）。
                # （报告记的 `IsADirectoryError` 已由 CR-09 移除：`Path("")` 走的是
                # 相对路径兜底分支，那条分支 2026-09-12 已删，见 image_source.py。）
                logger.warning(
                    "跳过 src 为空的 ImagePart（legacy 数据？attachment_id=%r）",
                    getattr(p, "attachment_id", None))
                continue
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
