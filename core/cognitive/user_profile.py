"""UserProfile — 用户画像（CognitiveLoop 侧类型）。

canonical home：core/cognitive/。DelegationPreparer（Task 9 迁 core 后同层）
只读消费此类型，不修改。workflow_engine schema 不再拥有此类型。
"""

from __future__ import annotations

from pydantic import BaseModel


class UserProfile(BaseModel):
    """用户画像（CognitiveLoop 管理）。

    DelegationPreparer 读取此信息来理解用户偏好和限制，
    但不修改——只读消费。

    TODO: 字段由 CognitiveLoop 侧定义。当前占位，防止裸 dict。
    """
    model_config = {"extra": "allow"}  # 暂时允许任意字段

    # 占位字段——供集成测试使用，后续由 CognitiveLoop 填充
    preferences: str | dict[str, str] = ""
    language: str = "zh"
    display_name: str = ""  # 用户显示名——来自 config.agent.user_name
