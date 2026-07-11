"""C8: 她自己的事。"""
from Module.system_prompt.protocol import Jinja2Section


class OwnLifeSection(Jinja2Section):
    name = "OwnLife"
    version = "1.0.0"
    description = "她自己的事：最近在干嘛"

    def __init__(self, template_path: str, session=None):
        super().__init__(template_path)
        self._session = session

    async def render(self) -> str:
        text = ""
        if self._session is not None:
            text = self._session.get_param("own_life", "")
        if not text:
            return ""
        return self._render_template(text=text)
