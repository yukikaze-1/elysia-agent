"""C6: 说话方式。"""
from Module.system_prompt.protocol import Jinja2Section


class SpeakingStyleSection(Jinja2Section):
    name = "SpeakingStyle"
    version = "3.0.0"
    description = "回复风格：五模式切换、声线、称呼、♪规则（sol_pro 基准）"

    def __init__(self, template_path: str):
        super().__init__(template_path)
        self._user_name = ""

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")

    async def render(self) -> str:
        return self._render_template(user_name=self._user_name)
