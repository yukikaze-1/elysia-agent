"""C3: 情感。"""
from typing import Any
from Module.system_prompt.protocol import Jinja2Section

class EmotionSection(Jinja2Section):
    name = "Emotion"
    version = "3.1.0"
    description = "当前心情 + 情绪响应规则（sol_pro 基准，含心理结构）"

    def __init__(self, template_path: str, session: Any = None):
        """初始化 Emotion section。

        Args:
            template_path: 模板文件路径。
            session: Session 实例，用于读取当前 mood。
        """
        super().__init__(template_path)
        self._session = session
        self._user_name = ""
        self._user_pronoun = "她"
        self._mood = "neutral"  # 可由 set_context(mood=...) 覆盖
        self._emotion_style = ""

    def set_context(self, **kwargs: Any) -> None:
        self._user_name = kwargs.get("user_name", "")
        self._user_pronoun = kwargs.get("user_pronoun", "她")
        if "mood" in kwargs:
            self._mood = kwargs["mood"]
        elif self._session is not None:
            self._mood = self._session.character_state.mood

    async def render(self) -> str:
        return self._render_template(
            # mood 是 LLM 生成的自由文本（<mood> 标签），user_name 是用户可控文本
            # ——两者都是不可信输入（CR-12 / S2-H2 同根）。
            mood=self._mood,
            user_name=self._user_name,
            user_pronoun=self._user_pronoun,
            emotion_style=self._emotion_style,
        )

    def set_character_profile(self, emotion_style: str) -> None:
        self._emotion_style = emotion_style
