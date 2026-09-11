"""WS 消息契约：character 域（ARCH-INC-P2-02 物理拆分）。

装配点见 `frontend_contract/ws_message/__init__.py`；该文件是唯一的
`ServerMessage`/`ClientMessage` 联合定义位置。
"""
from __future__ import annotations


from frontend_contract.base import ContractModel, TsNull
from frontend_contract.rest import CharacterBindingSnapshot, CharacterPublicStateView, ConversationUsage, FinishReason
from pydantic import ConfigDict, Discriminator, Field, model_validator
from typing import Annotated, Any, Literal, Optional, Union



class CharacterStateChangedMessage(ContractModel):
    """角色状态变化事件(架构稿 §12.4;v2,character_state_stream=true 后发送)。

    只含产品态字段;state 是完整 CharacterPublicStateView,不是 patch;未知 optional 字段省略。
    客户端只按同一 instance 内严格递增的 stateVersion 应用事件,不能用时间字符串替代顺序。
    """
    type: Literal["character.state.changed"] = "character.state.changed"
    conversationId: str = Field(min_length=1, description="会话 ID")
    characterId: str = Field(description="角色 ID")
    characterInstanceId: str = Field(description="角色实例 ID")
    characterRevision: int = Field(default=1, description="角色修订号")
    stateVersion: int = Field(default=1, description="实例状态版本")
    state: "CharacterPublicStateView" = Field(description="完整产品态视图(非 patch)")
