"""F3: TTS 合成标记。"""
from typing import Any
from Module.system_prompt.protocol import Jinja2Section


class TTSMarkupSection(Jinja2Section):
    name = "TTSMarkup"
    version = "2.0.0"
    description = "TTS 合成标记：语音语调、停顿、发音标注 + 使用限制"

    def set_context(self, **kwargs: Any) -> None:
        self._tts_provider = kwargs.get("tts_provider", "")

    async def render(self) -> str:
        tts_provider = getattr(self, '_tts_provider', '')
        return self._render_template(tts_provider=tts_provider)
