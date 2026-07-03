"""C2: 关系。"""
from Module.prompt.protocol import Jinja2Section


class RelationshipSection(Jinja2Section):
    name = "Relationship"
    version = "1.0.0"
    description = "关系：你和她的关系"

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
