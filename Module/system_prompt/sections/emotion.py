"""C3: 情感。"""
from Module.system_prompt.protocol import Jinja2Section


class EmotionSection(Jinja2Section):
    name = "Emotion"
    version = "3.1.0"
    description = "当前心情 + 情绪响应规则（sol_pro 基准，含心理结构）"

    def __init__(self, template_path: str, session=None):
        super().__init__(template_path)
        self._session = session
        self._user_name = ""
        self._user_pronoun = "她"
        self._mood = "neutral"  # 可由 set_context(mood=...) 覆盖

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")
        self._user_pronoun = kwargs.get("user_pronoun", "她")
        if "mood" in kwargs:
            self._mood = kwargs["mood"]

    async def render(self) -> str:
        return self._render_template(
            mood=self._mood, user_name=self._user_name,
            user_pronoun=self._user_pronoun,
        )
