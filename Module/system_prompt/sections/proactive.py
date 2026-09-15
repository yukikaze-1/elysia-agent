"""C7: 主动性。"""
from typing import Any
from Module.system_prompt.protocol import Jinja2Section

class ProactiveSection(Jinja2Section):
    name = "Proactive"
    version = "2.0.0"
    description = "主动性：何时主动说话、冷却机制"

    def __init__(self, template_path: str):
        """初始化 Proactive section。

        Args:
            template_path: 模板文件路径。
        """
        super().__init__(template_path)
        self._user_name = ""
        self._user_pronoun = "她"
        self._conversation_initiative = ""

    def set_context(self, **kwargs: Any) -> None:
        self._user_name = kwargs.get("user_name", "")
        self._user_pronoun = kwargs.get("user_pronoun", "她")

    async def render(self) -> str:
        return self._render_template(
            user_name=self._user_name,
            user_pronoun=self._user_pronoun,
            conversation_initiative=self._conversation_initiative,
        )

    def set_character_profile(self, conversation_initiative: str) -> None:
        self._conversation_initiative = conversation_initiative
