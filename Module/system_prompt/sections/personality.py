"""C1: 人格。"""
from typing import Any
from Module.system_prompt.protocol import Jinja2Section
from Module.system_prompt.sections.authored import Trusted

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

    @property
    def character_name(self) -> str:
        """本 section 渲染用的角色名（公开可写）。

        回合换绑时由 `core/prompt/prompt_assembler.py` 写入——此前直接赋值
        `section._name`，把「角色名存在哪个字段」这一内部细节泄漏给了消费方。
        注意与基类 `name`（section 自身标识，如 "Personality"）不是一回事。
        """
        return self._name

    @character_name.setter
    def character_name(self, value: str) -> None:
        self._name = value

    def set_context(self, **kwargs: Any) -> None:
        self._user_name = kwargs.get("user_name", "")
        self._user_pronoun = kwargs.get("user_pronoun", "她")

    async def render(self) -> str:
        # 转义已收口到 `Jinja2Section._render_template`（含 name / examples 的字符串
        # 叶子）——此处**不再**手工转义，否则会二次转义成 `&amp;amp;`。
        #
        # `core_personality` 的唯一例外：内建角色（`_authored=False` ⇔
        # `definition.source == "builtin"`）的 L1 来自仓库内 `character/<role>/`，
        # **不是**用户输入且不可编辑（`update()` 对 builtin 抛 CharacterImmutableError）。
        # 它的正文里本就含 `<voice>`/`<message>`/`<facts>` 这类**说明性**标签引用——
        # 一律转义会把 prompt 正文写成 `&lt;voice&gt;`（system prompt 快照测试当场抓到）。
        # 故此处用 `Trusted` **显式**声明"这段是受信的、原样保留"：豁免可见、可 grep，
        # 而"忘记转义"不再是默认结果。
        return self._render_template(
            name=self._name,
            core_personality=(
                self._core if self._authored else Trusted(self._core)
            ),
            # L2「自我」由 CharacterReflector 的 LLM 反思产物演化而来——不可信文本
            self_personality=self._self,
            examples=[
                {"user": user, "assistant": assistant}
                for user, assistant in self._examples
            ],
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
