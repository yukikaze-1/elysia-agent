"""C6: 说话方式。"""
from Module.prompt.protocol import Jinja2Section


class SpeakingStyleSection(Jinja2Section):
    name = "SpeakingStyle"
    version = "1.0.0"
    description = "说话方式：长度/节奏/语气"

    def __init__(self, template_path: str):
        super().__init__(template_path)
        self._user_name = ""

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")

    async def render(self) -> str:
        return self._render_template(user_name=self._user_name)
