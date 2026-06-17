"""PromptAssembler — builds the final messages[] for LLM calls.

Combines: CharacterConfig + SessionMemory + LongTermMemory + Brain.j2 template.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from Module.prompt import PromptManager
from Module.schema.message_format import content_to_openai, content_text
from .character_config import CharacterConfig
from .memory.session import SessionMemory
from .memory.long_term import LongTermMemory, MemoryEntry
from .emotion_engine import stt_emotion_emoji, stt_event_emoji

logger = logging.getLogger(__name__)


# Prompt/ template directory, relative to project root.
_PROMPT_DIR = str(Path(__file__).resolve().parent.parent / "prompt")


class PromptAssembler:
    """Assembles the full message context for each LLM call.

    Uses Brain.j2 which injects a dual-track thinking protocol
    (internal monologue → external response) into the system prompt.
    The LLM outputs only the external response as plain text.
    """

    def __init__(
        self,
        character: CharacterConfig,
        session: SessionMemory,
        long_term: LongTermMemory,
    ):
        self._character = character
        self._session = session
        self._long_term = long_term
        try:
            self._prompt = PromptManager(_PROMPT_DIR)
        except FileNotFoundError:
            logger.warning("模板目录不存在: %s，将使用 fallback", _PROMPT_DIR)
            self._prompt = None

    async def build_system_prompt(self, user_message: str = "", user_name: str = "妖梦",
                                   stt_emotion: str = "", stt_events: list[str] | None = None) -> str:
        """Render the system prompt from Brain.j2 and current state."""
        from datetime import datetime
        now = datetime.now()

        mood = self._session.get_param("mood", "neutral")

        perception = f"{user_name}说：{user_message[:200]}"

        sensory = {
            "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "time_of_day": self._time_of_day(now.hour),
            "day_of_week": self._day_name(now.weekday()),
            "season": self._season(now.month),
            "latency": 0.0,
            "perception": perception,
            "stt_emotion": stt_emotion,
            "stt_emotion_emoji": stt_emotion_emoji(stt_emotion) if stt_emotion else "",
            "stt_events": _format_events(stt_events) if stt_events else "",
        }

        recalled = await self._get_recalled(user_message)
        memory = {"micro": [], "macro": []}
        for entry in recalled:
            memory["micro"].append({
                "content": entry.text,
                "type": entry.memory_type,
                "timestamp": int(entry.timestamp) if entry.timestamp else int(now.timestamp()),
                "poignancy": entry.poignancy,
            })

        state = {"mood": mood}

        if self._prompt is not None:
            try:
                return self._prompt.render_macro(
                    "Brain.j2", "BrainSystemPrompt",
                    l3_personality_block=self._character.personality_block(),
                    sensory=sensory,
                    memory=memory,
                    state=state,
                )
            except Exception:
                logger.warning("Brain.j2 渲染失败，使用 fallback", exc_info=True)
        return await self._render_fallback(user_name, user_message, now, mood)
    
    
    async def _get_recalled(self, query: str = "") -> "list[MemoryEntry]":
        """Search long-term memory with the user's message as query."""
        
        results = await self._long_term.search(query_text=query, top_k=5)
        if results:
            logger.info("记忆召回: %d 条", len(results))
        return results if results else []

    async def build_messages(self, user_message: str = "",
                              user_name: str = "妖梦",
                              stt_emotion: str = "",
                              stt_events: list[str] | None = None,
                              content: list[dict] | None = None,
                              supports_multimodal: bool = False) -> list[dict]:
        """Build complete messages[] for the LLM API call.

        content: 多模态内容数组 (ChatMessageV2 content dicts)。
                 纯文本时可为 None，自动从 user_message 构建。
        """
        # 文本摘要给 system prompt 渲染用
        text = content_text_from_raw(content) if content else user_message
        system = await self.build_system_prompt(
            user_message=text, user_name=user_name,
            stt_emotion=stt_emotion, stt_events=stt_events,
        )
        messages: list[dict] = [{"role": "system", "content": system}]
        recent = self._session.recent_messages(8)
        for msg in recent:
            role = msg.get("role", "")

            # 工具结果消息 — 跳过，LLM 不需要（摘要已注入到 assistant 消息中）
            if role == "tool":
                continue

            # assistant 消息可能带 tool_calls — 提取摘要
            tool_calls = msg.get("tool_calls")
            tool_summary = _format_tool_summary(tool_calls) if tool_calls else ""

            content_raw = msg.get("content", "")
            if isinstance(content_raw, list):
                # ChatMessageV2 格式
                if supports_multimodal:
                    # 多模态 LLM — 图片原生传输
                    msg_content = content_to_openai(content_raw)
                    if tool_summary:
                        if isinstance(msg_content, list):
                            msg_content.append({"type": "text", "text": tool_summary})
                        else:
                            msg_content = msg_content + tool_summary
                    messages.append({
                        "role": role,
                        "content": msg_content,
                    })
                else:
                    # 纯文本 LLM — 剥离图片，提取纯文本后注入历史提示
                    text_only = [p for p in content_raw if p.get("type") == "text"]
                    text_content = "".join(p.get("content", "") for p in text_only)
                    image_count = sum(1 for p in content_raw if p.get("type") == "image")
                    if image_count > 0 and role == "user":
                        hint = _format_image_history_hint(image_count)
                        text_content = hint + text_content if text_content else hint
                    if text_content or tool_summary:
                        msg_content = (text_content or "") + (tool_summary or "")
                        messages.append({"role": role, "content": msg_content})
            elif isinstance(content_raw, str):
                msg_content = content_raw + (tool_summary or "")
                messages.append({"role": role, "content": msg_content})
            elif "parts" in msg:
                # 旧格式 — parts 数组
                text_from_parts = "".join(p["content"] for p in msg["parts"])
                msg_content = text_from_parts + (tool_summary or "")
                messages.append({"role": role, "content": msg_content})
            elif tool_summary:
                messages.append({"role": role, "content": tool_summary})
            else:
                messages.append({"role": role, "content": str(content_raw)})

        # 当前用户消息
        if content:
            has_images = any(p.get("type") == "image" for p in content)
            if has_images and not supports_multimodal:
                # 纯文本 LLM → 注入工具提示，不发 image_url
                text = content_text_from_raw(content)
                image_count = sum(1 for p in content if p.get("type") == "image")
                if image_count == 1:
                    hint = "[用户发送了一张图片。使用 describe_image 工具查看。]"
                else:
                    indices = list(range(image_count))
                    hint = f"[用户发送了{image_count}张图片。使用 describe_image(image_indices={indices}) 一次性查看所有图片。]"
                user_text = f"{hint}\n{text}" if text else hint
                messages.append({"role": "user", "content": user_text})
            else:
                messages.append({"role": "user", "content": content_to_openai(content)})
        else:
            messages.append({"role": "user", "content": user_message})
        return messages

    async def _render_fallback(self, user_name: str, user_message: str, now, mood: str) -> str:
        """Render system prompt without Jinja2 template (fallback)."""
        lines = [
            f"你是 {self._character.name}，一个桌面AI伴侣。",
            "",
            f"## 性格",
            self._character.personality_block(),
            "",
            "## 规则",
            "1. 回答简洁，2-4句话为主，像真实对话而非写文章。",
            "2. 用口语化表达，可以加语气词。",
            "",
            f"用户叫 {user_name}。",
            f"当前心情: {mood}。",
        ]
        recalled = await self._get_recalled(user_message)
        if recalled:
            lines.append("\n## 相关记忆")
            for r in recalled:
                lines.append(f"- {r}")
        lines.append("")
        lines.append("# 输出格式")
        lines.append("当需要展示代码、列表、表格等结构化内容时，先输出对话文本，")
        lines.append("然后把结构化内容放在最后，用以下标记包裹：")
        lines.append("<|markdown|>markdown内容</|markdown|>")
        lines.append("<|code|语言>代码</|code|>")
        lines.append("如果只是纯对话，不要使用任何标记。")
        return "\n".join(lines)

    @staticmethod
    def _day_name(weekday: int) -> str:
        names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return names[weekday]

    @staticmethod
    def _time_of_day(hour: int) -> str:
        if 5 <= hour < 12:
            return "morning"
        elif 12 <= hour < 14:
            return "noon"
        elif 14 <= hour < 18:
            return "afternoon"
        elif 18 <= hour < 22:
            return "evening"
        else:
            return "night"

    @staticmethod
    def _season(month: int) -> str:
        if 3 <= month <= 5:
            return "spring"
        elif 6 <= month <= 8:
            return "summer"
        elif 9 <= month <= 11:
            return "autumn"
        else:
            return "winter"


from .text_utils import content_text_from_content as content_text_from_raw  # noqa: F401 — 别名留存


def _format_events(events: list[str]) -> str:
    """格式化音频事件列表: 'Laughter 😀, Cry 😭'"""
    parts = []
    for evt in events:
        emoji = stt_event_emoji(evt)
        parts.append(f"{evt} {emoji}" if emoji else evt)
    return ", ".join(parts)


def _format_tool_summary(tool_calls: list[dict]) -> str:
    """将工具调用列表格式化为 LLM 上下文摘要。

    例如: describe_image(image_index=0, image_index=1), web_search(query="xxx")
    """
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
        # 展示参数名和值，精简输出
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
