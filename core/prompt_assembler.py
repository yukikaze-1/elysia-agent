"""PromptAssembler — builds the final messages[] for LLM calls.

Combines: CharacterConfig + SessionMemory + LongTermMemory + system template.

Two modes:
  - single_track (default): character/system.j2 → plain text response
  - dual_track: prompt/Brain.j2 → JSON {public_reply, inner_voice, mood}
"""

from __future__ import annotations

from pathlib import Path

from Module.prompt import PromptManager
from .character_config import CharacterConfig
from .memory.session import SessionMemory
from .memory.long_term import LongTermMemory


SYSTEM_PROMPT_TEMPLATE = """你是 {{ name }}，一个桌面AI伴侣。

## 性格
{{ personality_block }}

## 规则
1. 回答简洁，2-4句话为主，像真实对话而非写文章。
2. 用口语化表达，可以加语气词。
3. 如果用户问的问题需要查记忆或工具，先使用工具搜索再回答。
4. 保持角色一致性，不要突然变成客服或机器人口吻。

## 当前上下文
现在时间是 {{ current_time }}，今天是 {{ day_of_week }}。
用户叫 {{ user_name }}。

## 相关记忆
{% if recalled_memories %}
{% for m in recalled_memories %}
- {{ m }}
{% endfor %}
{% else %}
（暂无相关历史记忆）
{% endif %}

## 你的心情
{{ mood }}

## 最近对话
{% if recent_conversation %}
{% for msg in recent_conversation %}
- [{{ msg.role }}]: {{ msg.content }}
{% endfor %}
{% endif %}
"""

# Prompt/ template directory, relative to project root.
_PROMPT_DIR = str(Path(__file__).resolve().parent.parent / "prompt")


class PromptAssembler:
    """Assembles the full message context for each LLM call."""

    def __init__(
        self,
        character: CharacterConfig,
        session: SessionMemory,
        long_term: LongTermMemory,
        *,
        dual_track: bool = False,
    ):
        self._character = character
        self._session = session
        self._long_term = long_term
        self._dual_track = dual_track

        self._prompt = PromptManager("./character")
        if dual_track:
            self._brain_prompt = PromptManager(_PROMPT_DIR)
        else:
            self._brain_prompt = None

    @property
    def dual_track(self) -> bool:
        return self._dual_track

    async def build_system_prompt(self, user_message: str = "", user_name: str = "妖梦") -> str:
        """Render the system prompt from template and current state."""
        from datetime import datetime
        now = datetime.now()

        mood = self._session.get_param("mood", "neutral")
        recent = self._session.recent_messages(10)

        if self._dual_track:
            return await self._render_brain(now, user_message, user_name, mood, recent)

        try:
            return self._prompt.render(
                "system.j2",
                name=self._character.name,
                personality_block=self._character.personality_block(),
                current_time=now.strftime("%H:%M"),
                day_of_week=self._day_name(now.weekday()),
                user_name=user_name,
                recalled_memories=await self._get_recalled(user_message),
                mood=mood,
                recent_conversation=recent,
            )
        except FileNotFoundError:
            return await self._render_fallback(user_name, user_message, now, mood, recent)

    async def _render_brain(self, now, user_message: str, user_name: str, mood: str,
                            recent: list) -> str:
        """Render Brain.j2 BrainSystemPrompt macro for dual-track mode."""
        sensory = {
            "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "time_of_day": self._time_of_day(now.hour),
            "day_of_week": self._day_name(now.weekday()),
            "season": self._season(now.month),
            "latency": 0.0,
            "perception": f"{user_name}说：{user_message[:200]}",
        }

        # Convert recalled memories to Brain.j2 micro/macro format
        recalled = await self._get_recalled(user_message)
        memory = {"micro": [], "macro": []}
        for text in recalled:
            memory["micro"].append({
                "content": text,
                "timestamp": int(now.timestamp()),
                "poignancy": 5,
            })

        state = {"mood": mood}

        return self._brain_prompt.render_macro(
            "Brain.j2", "BrainSystemPrompt",
            l3_personality_block=self._character.personality_block(),
            sensory=sensory,
            memory=memory,
            state=state,
        )

    async def _get_recalled(self, query: str = "") -> list[str]:
        """Search long-term memory with the user's message as query."""
        results = await self._long_term.search(query_text=query, top_k=5)
        return [r.text for r in results] if results else []

    async def build_messages(self, user_message: str, user_name: str = "妖梦") -> list[dict]:
        """Build complete messages[] for the LLM API call.

        In dual_track mode, appends a prefix assistant message to force
        the LLM to start its response with {"public_reply": ".
        """
        system = await self.build_system_prompt(user_message=user_message, user_name=user_name)
        messages: list[dict] = [{"role": "system", "content": system}]
        recent = self._session.recent_messages(8)
        for msg in recent:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_message})
        return messages

    async def _render_fallback(self, user_name: str, user_message: str, now, mood: str,
                               recent: list) -> str:
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
