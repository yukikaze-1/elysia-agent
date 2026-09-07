"""C1: 人格。"""
from typing import Any
from Module.system_prompt.protocol import Jinja2Section
from Module.system_prompt.sections.authored import escape_authored_prompt


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
        self._authored = False
        self._examples: tuple[tuple[str, str], ...] = ()

    def set_context(self, **kwargs: Any) -> None:
        self._user_name = kwargs.get("user_name", "")
        self._user_pronoun = kwargs.get("user_pronoun", "她")

    async def render(self) -> str:
        core = (
            escape_authored_prompt(self._core)
            if self._authored else self._core
        )
        examples = [
            {
                "user": escape_authored_prompt(user),
                "assistant": escape_authored_prompt(assistant),
            }
            for user, assistant in self._examples
        ]
        return self._render_template(
            name=self._name,
            core_personality=core,
            self_personality=self._self,
            examples=examples,
        )

    def set_authored_profile(
        self, *, authored: bool, examples: tuple[tuple[str, str], ...]
    ) -> None:
        """Set the current revision's trust boundary and few-shot examples."""
        self._authored = authored
        self._examples = examples

    def update_personality(self, core: str, self_p: str) -> None:
        """更新人格数据。

        由 PromptAssembler 调用，传入 PersonalityEngine 的实时数据。

        Args:
            core: L1 Core 人格文本（通常不变）。
            self_p: L2 Self 人格文本（会话内缓慢演化）。
        """
        self._core = core
        self._self = self_p
