"""C3: 情感。"""
from Module.system_prompt.protocol import Jinja2Section


class EmotionSection(Jinja2Section):
    name = "Emotion"
    version = "2.0.0"
    description = "当前心情 + 情绪响应规则（对齐 Elysia 人格）"

    def __init__(self, template_path: str, session=None):
        super().__init__(template_path)
        self._session = session
        self._user_name = ""
        self._user_pronoun = "她"

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")
        self._user_pronoun = kwargs.get("user_pronoun", "她")

    async def render(self) -> str:
        mood = "neutral"
        if self._session is not None:
            mood = self._session.get_param("mood", "neutral")
        return self._render_template(
            mood=mood, user_name=self._user_name,
            user_pronoun=self._user_pronoun,
        )
