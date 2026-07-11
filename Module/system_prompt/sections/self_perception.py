"""C9: 自我感知。"""
from Module.system_prompt.protocol import Jinja2Section


class SelfPerceptionSection(Jinja2Section):
    name = "SelfPerception"
    version = "1.0.0"
    description = "自我感知：注意到的自身变化"

    def __init__(self, template_path: str, session=None):
        super().__init__(template_path)
        self._session = session

    async def render(self) -> str:
        if self._session is None:
            return ""
        history = self._session.get_param("self_perception_history", [])
        if not history:
            return ""
        content = history[-1].get("content", "")
        if not content:
            return ""
        return self._render_template(self_perception=content)
