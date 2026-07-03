"""PromptAssembler — builds the final messages[] for LLM calls.

Glue layer: prepares runtime data (sensory, memory, mood) and delegates
prompt assembly to Module.prompt.MessageBuilder.
"""

from __future__ import annotations

import logging
from pathlib import Path

from Module.prompt import PromptManager
from Module.prompt.registry import BrainPromptRegistry
from Module.prompt.message_builder import MessageBuilder
from .character_config import CharacterConfig
from .memory.session import SessionMemory
from .memory.long_term import LongTermMemory, MemoryEntry
from .emotion_engine import stt_emotion_emoji, stt_event_emoji
from .text_utils import content_text_from_content as content_text_from_raw

logger = logging.getLogger(__name__)

_PROMPT_DIR = str(Path(__file__).resolve().parent.parent / "prompt")
_BRAIN_DIR = str(Path(__file__).resolve().parent.parent / "prompt" / "brain")


class PromptAssembler:
    """Glue layer: prepares runtime context and delegates to MessageBuilder."""

    def __init__(
        self,
        character: CharacterConfig,
        session: SessionMemory,
        long_term: LongTermMemory,
    ):
        self._character = character
        self._session = session
        self._long_term = long_term
        self._tool_manager = None

        try:
            fallback = PromptManager(_PROMPT_DIR)
        except FileNotFoundError:
            logger.warning("模板目录不存在: %s，将使用 fallback", _PROMPT_DIR)
            fallback = None

        try:
            registry = BrainPromptRegistry(_BRAIN_DIR)
        except Exception:
            logger.warning("BrainPromptRegistry 加载失败", exc_info=True)
            registry = None

        self._builder = MessageBuilder(registry=registry, fallback_prompt=fallback)

    def set_tool_manager(self, tm) -> None:
        """注入 ToolManager，用于自动生成工具列表 prompt。"""
        self._tool_manager = tm

    async def build_system_prompt(self, user_message: str = "", user_name: str = "",
                                   stt_emotion: str = "", stt_events: list[str] | None = None) -> str:
        """Prepare runtime context, then delegate to MessageBuilder."""
        from datetime import datetime
        now = datetime.now()

        mood = self._session.get_param("mood", "neutral")

        parts = [f"{user_name}说：{user_message}"]
        if stt_emotion and stt_emotion != "NEUTRAL":
            parts.append(f"[语调: {stt_emotion}]")
        parts.append(f"[当前心情: {mood}]")
        perception = "\n".join(parts)

        sensory = {
            "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "time_of_day": self._time_of_day(now.hour),
            "day_of_week": self._day_name(now.weekday()),
            "season": self._season(now.month),
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
                "timestamp": entry.timestamp if entry.timestamp else now.timestamp(),
                "poignancy": entry.poignancy,
            })

        history = self._session.get_param("self_perception_history", [])
        self_perception = history[-1]["content"] if history else ""

        tool_list = ""
        if self._tool_manager:
            tool_list = self._tool_manager.to_prompt_text()

        context = {
            "name": self._character.name,
            "user_name": user_name,
            "user_preferences": "",
            "self_perception": self_perception,
            "sensory_ctx": sensory,
            "memory_ctx": memory,
            "mood": mood,
            "tool_list": tool_list,
            "session_id": self._session.session_id,
        }

        return self._builder.build_system_prompt("default", context)

    async def _get_recalled(self, query: str = "") -> "list[MemoryEntry]":
        """Search long-term memory with the user's message as query."""
        results = await self._long_term.search(query_text=query, top_k=5)
        if results:
            logger.info("记忆召回: %d 条", len(results))
        return results if results else []

    async def build_messages(self, user_message: str = "",
                              user_name: str = "",
                              stt_emotion: str = "",
                              stt_events: list[str] | None = None,
                              content: list[dict] | None = None,
                              supports_multimodal: bool = False) -> list[dict]:
        """Build complete messages[] for the LLM API call."""
        text = content_text_from_raw(content) if content else user_message
        system = await self.build_system_prompt(
            user_message=text, user_name=user_name,
            stt_emotion=stt_emotion, stt_events=stt_events,
        )
        history = self._session.recent_messages(8)
        user_content = content if content else user_message
        return self._builder.build_messages(
            system, history, user_content,
            supports_multimodal=supports_multimodal,
        )

    @staticmethod
    def _day_name(weekday: int) -> str:
        names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return names[weekday]

    @staticmethod
    def _time_of_day(hour: int) -> str:
        if 5 <= hour < 12:
            return "早晨"
        elif 12 <= hour < 14:
            return "中午"
        elif 14 <= hour < 18:
            return "下午"
        elif 18 <= hour < 22:
            return "傍晚"
        else:
            return "夜晚"

    @staticmethod
    def _season(month: int) -> str:
        if 3 <= month <= 5:
            return "春"
        elif 6 <= month <= 8:
            return "夏"
        elif 9 <= month <= 11:
            return "秋"
        else:
            return "冬"


def _format_events(events: list[str]) -> str:
    """格式化音频事件列表: 'Laughter 😀, Cry 😭'"""
    parts = []
    for evt in events:
        emoji = stt_event_emoji(evt)
        parts.append(f"{evt} {emoji}" if emoji else evt)
    return ", ".join(parts)
