"""PromptAssembler — builds the final messages[] for LLM calls.

Combines: CharacterConfig + SessionMemory + LongTermMemory + system template.
"""

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
3. 如果用户问的问题需要外部信息（比如时间、回忆），说明你需要查工具。
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


class PromptAssembler:
    """Assembles the full message context for each LLM call."""

    def __init__(
        self,
        character: CharacterConfig,
        session: SessionMemory,
        long_term: LongTermMemory,
    ):
        self._character = character
        self._session = session
        self._long_term = long_term
        self._prompt = PromptManager("./character")

    def build_system_prompt(self, user_name: str = "妖梦") -> str:
        """Render the system prompt from template and current state."""
        from datetime import datetime
        now = datetime.now()

        mood = self._session.get_param("mood", "neutral")
        recent = self._session.recent_messages(10)

        try:
            return self._prompt.render(
                "system.j2",
                name=self._character.name,
                personality_block=self._character.personality_block(),
                current_time=now.strftime("%H:%M"),
                day_of_week=self._day_name(now.weekday()),
                user_name=user_name,
                recalled_memories=self._get_recalled(),
                mood=mood,
                recent_conversation=recent,
            )
        except FileNotFoundError:
            return self._render_fallback(user_name, now, mood, recent)

    def _get_recalled(self) -> list[str]:
        """Search long-term memory. Returns top results as text lines."""
        results = self._long_term.search(query_text="")
        return [r.text for r in results[:5]] if results else []

    def build_messages(self, user_message: str, user_name: str = "妖梦") -> list[dict]:
        """Build complete messages[] for the LLM API call."""
        system = self.build_system_prompt(user_name=user_name)
        messages: list[dict] = [{"role": "system", "content": system}]
        recent = self._session.recent_messages(8)
        for msg in recent:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_message})
        return messages

    def _render_fallback(self, user_name: str, now, mood: str, recent: list) -> str:
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
        recalled = self._get_recalled()
        if recalled:
            lines.append("\n## 相关记忆")
            for r in recalled:
                lines.append(f"- {r}")
        return "\n".join(lines)

    @staticmethod
    def _day_name(weekday: int) -> str:
        names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return names[weekday]
