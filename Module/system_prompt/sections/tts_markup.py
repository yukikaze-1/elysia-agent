"""F3: TTS 合成标记。"""
from Module.system_prompt.protocol import Jinja2Section


class TTSMarkupSection(Jinja2Section):
    name = "TTSMarkup"
    version = "1.0.0"
    description = "TTS 合成标记：语音语调、停顿、发音标注"

    def set_context(self, **kwargs) -> None:
        self._tts_provider = kwargs.get("tts_provider", "")

    async def render(self) -> str:
        tts_provider = getattr(self, '_tts_provider', '')
        return self._render_template(tts_provider=tts_provider)
