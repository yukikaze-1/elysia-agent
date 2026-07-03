"""C1: 人格。"""
from Module.prompt.protocol import Jinja2Section


class PersonalitySection(Jinja2Section):
    name = "Personality"
    version = "1.0.0"
    description = "人格：行为模式定义"

    def __init__(self, template_path: str, character_name: str = ""):
        super().__init__(template_path)
        self._character_name = character_name
        self._user_name = ""

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")

    async def render(self) -> str:
        return self._render_template(
            name=self._character_name,
            user_name=self._user_name,
        )
