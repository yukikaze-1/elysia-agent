"""C6: 说话方式。"""
from Module.system_prompt.protocol import Jinja2Section


class SpeakingStyleSection(Jinja2Section):
    name = "SpeakingStyle"
    version = "3.1.0"
    description = "回复风格：五模式切换，支持 L2 驱动的动态演化"

    def __init__(self, template_path: str):
        """初始化 SpeakingStyle section。

        Args:
            template_path: 模板文件路径。
        """
        super().__init__(template_path)
        self._user_name = ""
        self._speaking_style_c6 = ""

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")
        self._speaking_style_c6 = kwargs.get("speaking_style_c6", "")

    async def render(self) -> str:
        return self._render_template(
            user_name=self._user_name,
            speaking_style_c6=self._speaking_style_c6,
        )
