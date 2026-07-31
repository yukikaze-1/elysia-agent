"""MessageBuilder — 纯 prompt 组装。"""

from __future__ import annotations

import json
import logging

from core.schema.message import Message
from core.schema.part import ImagePart, TextPart
from core.schema.tool import ToolCall
from core.schema.adapters.message_adapter import content_to_openai, content_text, parts_from_dicts

logger = logging.getLogger(__name__)


class MessageBuilder:
    """纯 prompt 组装 — 接收 system prompt + history → 输出 messages[]。

    无状态，依赖 core.schema 类型（Message, Part, ToolCall, adapters）。
    """

    def build_messages(
        self,
        system_prompt: str,
        history: list[Message],
        user_content: str | list[dict] | None = None,
        *,
        supports_multimodal: bool = False,
    ) -> list[dict]:
        """组装完整 messages[]。

        Args:
            system_prompt: 已渲染的 system prompt 字符串
            history: SessionMemory.recent_messages(8) 返回的 Message 列表
            user_content: 当前用户消息（str 或多模态 content list）
            supports_multimodal: LLM 是否支持多模态图片

        Returns:
            [{"role": "system", "content": system_prompt},
             ...history with inner_voice + tool_summary injection,
             {"role": "user", "content": user_content}]
        """
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        for msg in history:
            if msg.role == "tool":
                continue

            inner_voice = getattr(msg, "inner_voice", None)
            iv_prefix = f"<inner_voice>{inner_voice}</inner_voice>\n" if inner_voice else ""

            tool_calls = getattr(msg, "tool_calls", None)
            tool_summary = _format_tool_summary(tool_calls) if tool_calls else ""

            parts = msg.content  # list[Part]
            if supports_multimodal:
                msg_content = content_to_openai(list(parts))
                if tool_summary:
                    if isinstance(msg_content, list):
                        msg_content.append({"type": "text", "text": tool_summary})
                    else:
                        msg_content = msg_content + tool_summary
                if iv_prefix:
                    if isinstance(msg_content, list):
                        msg_content.insert(0, {"type": "text", "text": iv_prefix.rstrip()})
                    else:
                        msg_content = iv_prefix + msg_content
                messages.append({"role": msg.role, "content": msg_content})
            else:
                text_only = [p for p in parts if isinstance(p, TextPart)]
                text_content = "".join(p.content for p in text_only)
                image_count = sum(1 for p in parts if isinstance(p, ImagePart))
                if image_count > 0 and msg.role == "user":
                    hint = _format_image_history_hint(image_count)
                    text_content = hint + text_content if text_content else hint
                if text_content or tool_summary:
                    msg_content = (text_content or "") + (tool_summary or "")
                    messages.append({"role": msg.role, "content": iv_prefix + msg_content})

        # 当前用户消息
        if isinstance(user_content, list):
            user_parts = parts_from_dicts(user_content)
            has_images = any(isinstance(p, ImagePart) for p in user_parts)
            if has_images and not supports_multimodal:
                text_content = content_text(user_parts)
                image_count = sum(1 for p in user_parts if isinstance(p, ImagePart))
                if image_count == 1:
                    hint = "[用户发送了一张图片。使用 describe_image 工具查看。]"
                else:
                    indices = list(range(image_count))
                    hint = f"[用户发送了{image_count}张图片。使用 describe_image(image_indices={indices}) 一次性查看所有图片。]"
                user_text = f"{hint}\n{text_content}" if text_content else hint
                messages.append({"role": "user", "content": user_text})
            else:
                messages.append({"role": "user", "content": content_to_openai(user_parts)})
        elif user_content:
            messages.append({"role": "user", "content": user_content})

        return messages


def _format_tool_summary(tool_calls: list[ToolCall]) -> str:
    """将工具调用列表格式化为 LLM 上下文摘要。

    Args:
        tool_calls: ToolCall 对象列表。

    Returns:
        str: 格式化后的摘要字符串（如 " [已调用: search(query=...)]"），空列表时返回空字符串。
    """
    if not tool_calls:
        return ""
    names: list[str] = []
    for tc in tool_calls:
        name = tc.function.name
        try:
            parsed = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        # arguments 可能是合法 JSON 但非 dict（list/str/int），跳过避免 AttributeError
        args = parsed if isinstance(parsed, dict) else {}
        arg_strs = [f"{k}={v}" for k, v in args.items()]
        if arg_strs:
            names.append(f"{name}({', '.join(arg_strs)})")
        else:
            names.append(name)
    return f" [已调用: {', '.join(names)}]"


def _format_image_history_hint(count: int) -> str:
    """历史用户消息中图片的降级提示。

    Args:
        count: 图片数量。

    Returns:
        str: 降级提示文本（如 "[用户曾发送一张图片] "）。
    """
    if count == 1:
        return "[用户曾发送一张图片] "
    return f"[用户曾发送{count}张图片] "
