"""C1: 人格。"""
from typing import Any
from Module.system_prompt.protocol import Jinja2Section


class PersonalitySection(Jinja2Section):
    name = "Personality"
    version = "2.0.0"
    description = "人格：L1 Core + L2 Self（动态性格引擎）"

    def __init__(self, template_path: str, character_name: str = "",
                 core_personality: str = ""):
        """初始化 Personality section。

        Args:
            template_path: 模板文件路径。
            character_name: 角色名称。
            core_personality: L1 Core 人格文本。
        """
        super().__init__(template_path)
        self._core = core_personality
        self._self = ""
        self._name = character_name
        self._user_name = ""
        self._user_pronoun = "她"

    def set_context(self, **kwargs: Any) -> None:
        self._user_name = kwargs.get("user_name", "")
        self._user_pronoun = kwargs.get("user_pronoun", "她")

    async def render(self) -> str:
        return self._render_template(
            name=self._name,
            core_personality=self._core,
            self_personality=self._self,
        )

    def update_personality(self, core: str, self_p: str) -> None:
        """更新人格数据。

        由 PromptAssembler 调用，传入 PersonalityEngine 的实时数据。

        Args:
            core: L1 Core 人格文本（通常不变）。
            self_p: L2 Self 人格文本（会话内缓慢演化）。
        """
        self._core = core
        self._self = self_p
