"""MessageBuilder — 纯 prompt 组装，不依赖 core/。"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class MessageBuilder:
    """纯 prompt 组装 — 接收 system prompt + history → 输出 messages[]。

    无状态，不依赖 core/ 的任何组件。
    """

    def build_messages(
        self,
        system_prompt: str,
        history: list[dict],
        user_content: str | list[dict] | None = None,
        *,
        supports_multimodal: bool = False,
    ) -> list[dict]:
        """组装完整 messages[]。

        Args:
            system_prompt: 已渲染的 system prompt 字符串
            history: SessionMemory.recent_messages(8) 返回的历史消息列表
            user_content: 当前用户消息（str 或多模态 content list）
            supports_multimodal: LLM 是否支持多模态图片

        Returns:
            [{"role": "system", "content": system_prompt},
             ...history with inner_voice + tool_summary injection,
             {"role": "user", "content": user_content}]
        """
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        for msg in history:
            role = msg.get("role", "")
            if role == "tool":
                continue

            inner_voice = msg.get("inner_voice")
            iv_prefix = f"<inner_voice>{inner_voice}</inner_voice>\n" if inner_voice else ""

            tool_calls = msg.get("tool_calls")
            tool_summary = _format_tool_summary(tool_calls) if tool_calls else ""

            content_raw = msg.get("content", "")
            if isinstance(content_raw, list):
                if supports_multimodal:
                    msg_content = _content_to_openai(content_raw)
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
                    messages.append({"role": role, "content": msg_content})
                else:
                    text_only = [p for p in content_raw if p.get("type") == "text"]
                    text_content = "".join(p.get("content", "") for p in text_only)
                    image_count = sum(1 for p in content_raw if p.get("type") == "image")
                    if image_count > 0 and role == "user":
                        hint = _format_image_history_hint(image_count)
                        text_content = hint + text_content if text_content else hint
                    if text_content or tool_summary:
                        msg_content = (text_content or "") + (tool_summary or "")
                        messages.append({"role": role, "content": iv_prefix + msg_content})
            elif "parts" in msg:
                text_from_parts = "".join(p["content"] for p in msg["parts"])
                msg_content = text_from_parts + (tool_summary or "")
                messages.append({"role": role, "content": iv_prefix + msg_content})
            elif isinstance(content_raw, str):
                msg_content = content_raw + (tool_summary or "")
                messages.append({"role": role, "content": iv_prefix + msg_content})
            elif tool_summary:
                messages.append({"role": role, "content": iv_prefix + tool_summary})
            else:
                messages.append({"role": role, "content": iv_prefix + str(content_raw)})

        # 当前用户消息
        if isinstance(user_content, list):
            has_images = any(p.get("type") == "image" for p in user_content)
            if has_images and not supports_multimodal:
                text = _content_text(user_content)
                image_count = sum(1 for p in user_content if p.get("type") == "image")
                if image_count == 1:
                    hint = "[用户发送了一张图片。使用 describe_image 工具查看。]"
                else:
                    indices = list(range(image_count))
                    hint = f"[用户发送了{image_count}张图片。使用 describe_image(image_indices={indices}) 一次性查看所有图片。]"
                user_text = f"{hint}\n{text}" if text else hint
                messages.append({"role": "user", "content": user_text})
            else:
                messages.append({"role": "user", "content": _content_to_openai(user_content)})
        elif user_content:
            messages.append({"role": "user", "content": user_content})

        return messages


def _content_to_openai(content: list[dict]) -> str | list[dict]:
    """将消息 content（list[dict]）转为 OpenAI 格式。边界处 dict→Part。"""
    from core.schema.adapters.message_adapter import content_to_openai, parts_from_dicts
    return content_to_openai(parts_from_dicts(content))


def _content_text(content: list[dict]) -> str:
    """从消息 content（list[dict]）提取纯文本。边界处 dict→Part。"""
    from core.schema.adapters.message_adapter import content_text, parts_from_dicts
    return content_text(parts_from_dicts(content))


def _format_tool_summary(tool_calls: list[dict]) -> str:
    """将工具调用列表格式化为 LLM 上下文摘要。"""
    if not tool_calls:
        return ""
    names: list[str] = []
    for tc in tool_calls:
        func = tc.get("function", {})
        name = func.get("name", "unknown")
        try:
            args = json.loads(func.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        arg_strs = [f"{k}={v}" for k, v in args.items()]
        if arg_strs:
            names.append(f"{name}({', '.join(arg_strs)})")
        else:
            names.append(name)
    return f" [已调用: {', '.join(names)}]"


def _format_image_history_hint(count: int) -> str:
    """历史用户消息中图片的降级提示。"""
    if count == 1:
        return "[用户曾发送一张图片] "
    return f"[用户曾发送{count}张图片] "
