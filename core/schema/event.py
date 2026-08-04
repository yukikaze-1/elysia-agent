"""事件数据类型 — EventBus 的 Pydantic 模型。

每个模型自带 event_type discriminator，形成 EventData union。
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from core.schema.types import SessionId


class InterruptData(BaseModel):
    event_type: Literal["interrupt"] = "interrupt"
    source: str = Field(default="?")


class StateChangeData(BaseModel):
    event_type: Literal["state_change"] = "state_change"
    state: str
    request_id: str


class UserInputData(BaseModel):
    event_type: Literal["user_input"] = "user_input"
    text: str
    user_name: str
    request_id: str
    content: list[dict] | None = None
    emotion: str = ""
    events: list[str] = Field(default_factory=list)
    # 批次 2（v2 消息）：预分配用户消息 ID（ACK 一致）+ 来源协议版本
    message_id: str | None = None
    protocol_version: int = 1
    # 批次 3（multi-session）：可选会话 ID，None = 未归属
    conversation_id: SessionId | None = None


class SystemTickData(BaseModel):
    event_type: Literal["system_tick"] = "system_tick"
    reason: Literal["silence_timeout", "time_of_day"]
    elapsed: float | None = None
    time: str | None = None


class TaskResultData(BaseModel):
    event_type: Literal["task_result"] = "task_result"
    task_id: str
    result: str
    original_intent: str
    dispatched_at: str
    is_error: bool = False


EventData = Annotated[
    InterruptData | StateChangeData | UserInputData | SystemTickData | TaskResultData,
    Field(discriminator="event_type"),
]
