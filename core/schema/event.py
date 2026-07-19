"""事件数据类型 — EventBus 的 Pydantic 模型。

每个模型自带 event_type discriminator，形成 EventData union。
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field


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


EventData = Annotated[
    InterruptData | StateChangeData | UserInputData | SystemTickData | TaskResultData,
    Field(discriminator="event_type"),
]
