"""F1: 冲突规则。"""
from Module.system_prompt.protocol import Jinja2Section


class ConflictRulesSection(Jinja2Section):
    name = "ConflictRules"
    version = "2.0.0"
    description = "冲突规则：安全边界 + 硬约束优先 + inner_voice 规则"
