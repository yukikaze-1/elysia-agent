"""C9: 自我感知。"""
from Module.system_prompt.protocol import Jinja2Section


class SelfPerceptionSection(Jinja2Section):
    name = "SelfPerception"
    version = "2.0.0"
    description = "自我感知：AwarenessTrigger 条件激活"

    def __init__(self, template_path: str):
        """初始化 SelfPerception section。

        Args:
            template_path: 模板文件路径。
        """
        super().__init__(template_path)
        self._awareness_text = ""

    def update_awareness(self, text: str) -> None:
        """更新自我感知文本。

        由 PromptAssembler 调用，传入 PersonalityEngine 的 awareness 数据。

        Args:
            text: awareness 文本（空字符串时 C9 不渲染）。
        """
        self._awareness_text = text

    async def render(self) -> str:
        if not self._awareness_text:
            return ""
        return self._render_template(awareness_text=self._awareness_text)
