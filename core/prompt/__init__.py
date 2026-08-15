"""core/prompt/ — System prompt 组装层。

CORE-MOD-P1-02 从 core/adapter 迁入：PromptAssembler 是应用级胶水
（依赖 CharacterEngine 与记忆域），不属于后端适配器层。
"""

from core.prompt.message_builder import MessageBuilder
from core.prompt.prompt_assembler import PromptAssembler

__all__ = ["MessageBuilder", "PromptAssembler"]
