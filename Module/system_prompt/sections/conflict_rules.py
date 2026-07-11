"""F1: 冲突规则。"""
from Module.system_prompt.protocol import Jinja2Section


class ConflictRulesSection(Jinja2Section):
    name = "ConflictRules"
    version = "1.1.0"
    description = "冲突规则：框架硬约束优先于角色软约束"
