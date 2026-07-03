"""F2: 输出格式。"""
from Module.prompt.protocol import Jinja2Section


class OutputFormatSection(Jinja2Section):
    name = "OutputFormat"
    version = "1.0.0"
    description = "输出格式：inner_voice 双轨协议"

    async def render(self) -> str:
        return self._render_template()
