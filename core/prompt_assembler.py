"""PromptAssembler — builds the final messages[] for LLM calls.

Combines: CharacterConfig + SessionMemory + LongTermMemory + Brain.j2 template.
"""

from __future__ import annotations

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
                              content: list[dict] | None = None) -> list[dict]:
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
            content_raw = msg.get("content", "")
            if isinstance(content_raw, list):
                # ChatMessageV2 格式 — 历史只取文本部分。
                # 图片/文件等非文本 part 对文本 LLM 无意义，且会导致
                # DeepSeek 等纯文本 provider 报 400 (unknown variant image_url)。
                text_only = [p for p in content_raw if p.get("type") == "text"]
                if text_only:
                    messages.append({
                        "role": msg["role"],
                        "content": content_to_openai(text_only),
                    })
                # 纯图片消息（无文本）跳过，不贡献上下文
            elif isinstance(content_raw, str):
                messages.append({"role": msg["role"], "content": content_raw})
            elif "parts" in msg:
                # 旧格式 — parts 数组
                text_from_parts = "".join(p["content"] for p in msg["parts"])
                messages.append({"role": msg["role"], "content": text_from_parts})
            else:
                messages.append({"role": msg["role"], "content": str(content_raw)})

        # 当前用户消息
        if content:
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


def content_text_from_raw(content: list[dict] | str | None) -> str:
    """从原始 content 提取纯文本（给 system prompt 渲染用）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(
        p.get("content", "") for p in content
        if isinstance(p, dict) and p.get("type") == "text"
    )


def _format_events(events: list[str]) -> str:
    """格式化音频事件列表: 'Laughter 😀, Cry 😭'"""
    parts = []
    for evt in events:
        emoji = stt_event_emoji(evt)
        parts.append(f"{evt} {emoji}" if emoji else evt)
    return ", ".join(parts)
