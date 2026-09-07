"""C2: 关系。"""
from typing import Any
from Module.system_prompt.protocol import Jinja2Section
from Module.system_prompt.sections.authored import escape_authored_prompt


class RelationshipSection(Jinja2Section):
    name = "Relationship"
    version = "3.0.0"
    description = "关系：主动靠近不占有、四阶段发展、尊重边界（sol_pro 基准）"

    def __init__(self, template_path: str, character_name: str = ""):
        """初始化 Relationship section。

        Args:
            template_path: 模板文件路径。
            character_name: 角色名称。
        """
        super().__init__(template_path)
        self._character_name = character_name
        self._user_name = ""
        self._user_pronoun = "她"
        self._relationship_style = ""

    def set_context(self, **kwargs: Any) -> None:
        self._user_name = kwargs.get("user_name", "")
        self._user_pronoun = kwargs.get("user_pronoun", "她")

    async def render(self) -> str:
        return self._render_template(
            name=self._character_name,
            user_name=self._user_name,
            user_pronoun=self._user_pronoun,
            relationship_style=escape_authored_prompt(self._relationship_style),
        )

    def set_character_profile(self, *, name: str, relationship_style: str) -> None:
        self._character_name = name
        self._relationship_style = relationship_style
