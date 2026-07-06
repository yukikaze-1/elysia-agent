"""F1: 冲突规则。"""
from Module.prompt.protocol import StaticSection

_F1_TEXT = "<conflict_rules>\n角色定义你是谁、怎么说话。框架约束是硬规则。冲突时框架优先。\n</conflict_rules>"


class ConflictRulesSection(StaticSection):
    def __init__(self):
        super().__init__("ConflictRules", "1.0.0",
                         "冲突规则：框架优先于角色", _F1_TEXT)
