"""CharacterPreviewPort — 无状态角色预览推理口（BE-STAGEH-03）。

预览只做「按 draft 组装 prompt → 一次 LLM 调用」的纯推理。绝对零副作用边界：
不建/激活 session，不建 CharacterInstance，不写 message/generation archive，不读
或写会话历史/LTM/relationship/演化 state，不推进 definition revision 或 instance
stateVersion，不触发 reflector/compression/mood/TTS/audio/expression/Live2D，不调用
任何工具，也不改变 Agent 当前活动会话/角色。

用量不并入任何 conversation token 统计——预览结果不带 usage。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.adapter.llm_adapter import LLMAdapter
    from core.character.runtime_profile import RuntimeCharacterProfile
    from core.profile.user_identity import UserIdentity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreviewReply:
    """一次预览推理的归一化结果。"""

    text: str
    model: str | None = None


class CharacterPreviewPort(Protocol):
    """窄接口：只接收已编译 profile + 用户身份快照 + 一条输入。"""

    async def generate(
        self,
        *,
        profile: "RuntimeCharacterProfile",
        user_identity: "UserIdentity",
        user_message: str,
    ) -> PreviewReply: ...


class PromptCharacterPreviewAdapter:
    """用一次性 PromptAssembler（无会话/无记忆）组装 prompt，经 LLMAdapter 单次推理。

    每次调用新建 assembler——PromptAssembler 是有状态对象（set_character 会改写
    section），复用生产实例会把预览角色泄漏进真实会话的 prompt。
    """

    def __init__(self, llm: "LLMAdapter"):
        self._llm = llm

    async def generate(
        self,
        *,
        profile: "RuntimeCharacterProfile",
        user_identity: "UserIdentity",
        user_message: str,
    ) -> PreviewReply:
        from core.prompt.prompt_assembler import PromptAssembler

        assembler = PromptAssembler(
            character=profile.config,
            session=None,      # 无会话历史
            long_term=None,    # 无 LTM 检索（C4 空渲染）
        )
        assembler.enable_preview_mode()
        assembler.set_character(profile.config.name, profile.config.name, profile)
        messages = await assembler.build_messages(
            user_message=user_message,
            user_name=user_identity.displayName or "用户",
            user_pronoun_override=user_identity.pronouns,
        )
        result = await self._llm.think(messages)   # tools=None → 不挂任何工具
        return PreviewReply(text=result.text or "", model=self._llm.model)
