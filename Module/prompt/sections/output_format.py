"""F2: 输出格式。"""
from Module.prompt.protocol import Jinja2Section


class OutputFormatSection(Jinja2Section):
    name = "OutputFormat"
    version = "1.0.0"
    description = "输出格式：inner_voice 双轨协议 + TTS 标记指令"

    def set_context(self, **kwargs) -> None:
        self._tts_provider = kwargs.get("tts_provider", "")

    async def render(self) -> str:
        tts_provider = getattr(self, '_tts_provider', '')
        return self._render_template(tts_provider=tts_provider)
