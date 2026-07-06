"""C3: 情感。"""
from Module.prompt.protocol import Jinja2Section


class EmotionSection(Jinja2Section):
    name = "Emotion"
    version = "1.0.0"
    description = "当前心情 + 情绪传染规则"

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
