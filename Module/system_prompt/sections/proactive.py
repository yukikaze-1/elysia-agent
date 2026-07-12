"""C7: 主动性。"""
from Module.system_prompt.protocol import Jinja2Section


class ProactiveSection(Jinja2Section):
    name = "Proactive"
    version = "1.0.0"
    description = "主动性：何时主动说话、何时退一步"

    def __init__(self, template_path: str):
        super().__init__(template_path)
        self._user_name = ""

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")

    async def render(self) -> str:
        return self._render_template(user_name=self._user_name)
