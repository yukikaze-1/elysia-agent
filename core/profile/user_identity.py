"""UserIdentity — 用户"最小身份"模型。

架构稿决策 #7：『我』的最小身份是角色功能前置依赖。
首版不做完整用户画像，但 Prompt 所需的身份从角色/全局配置移出到 UserIdentity。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.schema.utils import now_iso

# 首页本地用户稳定 ID（架构稿 §5.1：首版可为确定性 usr_local_default）
LOCAL_DEFAULT_USER_ID = "usr_local_default"
LOCAL_DEFAULT_PRONOMS = "她"   # B1 决断：全局唯一，非 per-role
LOCAL_DEFAULT_LOCALE = "zh-CN"
LOCAL_DEFAULT_TIMEZONE = "Asia/Shanghai"


class UserIdentity(BaseModel):
    """用户最小身份。"""

    userId: str = LOCAL_DEFAULT_USER_ID
    displayName: str = ""                    # 用户显示名（来源 config.agent.user_name bootstrap）
    preferredAddress: str | None = None      # 希望角色通常如何称呼；空则用 displayName
    # 用户代词，可填 {{ user_pronoun }}（她/他/你）。
    # **可空**（Batch 3.5 / B3-CB-1）：契约 `UserIdentityProfile.pronouns` 本就是
    # `Optional[str] + TsNull`（wire 上 `string | null`），设计稿 §5.1 也把它列为
    # `string?`、§11.1 明确「省略 pronouns 表示清空」，而 `character_routes._me_put`
    # 正是把省略映射成 `None` 后落库。此前域模型写成非空 `str`，于是同一条链路上
    # “写入接受、读回抛 ValidationError”：PUT 自己 500、记录被持久污染、重启后
    # `connect()` 装载失败 ⇒ 身份永久不可用，且 `/ready` 因探的是 ping 而保持绿。
    # 缺字段仍取默认「她」（存量记录不受影响）；显式 null 才表示“已清空”。
    pronouns: str | None = LOCAL_DEFAULT_PRONOMS
    locale: str = LOCAL_DEFAULT_LOCALE
    timezone: str = LOCAL_DEFAULT_TIMEZONE
    initialized: bool = False                # 仅 UX 标记（用户是否已确认），不 gate pronouns
    defaultCharacterId: str | None = None    # 新会话默认角色
    revision: int = Field(default=1, ge=1)   # 乐观并发版本，从 1 起
    createdAt: str = Field(default_factory=now_iso)
    updatedAt: str = Field(default_factory=now_iso)

    def touch(self) -> None:
        """更新 updatedAt + 递增 revision（写前调用）。"""
        self.updatedAt = now_iso()
        self.revision += 1
