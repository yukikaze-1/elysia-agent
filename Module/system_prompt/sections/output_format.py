"""F2: 输出格式。"""
from Module.system_prompt.protocol import Jinja2Section


class OutputFormatSection(Jinja2Section):
    name = "OutputFormat"
    version = "2.0.0"
    description = "输出格式：inner_voice 双轨协议 + 任务优先规则"
