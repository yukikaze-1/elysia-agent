"""事件数据类型 — EventBus 的 Pydantic 模型。

每个模型自带 event_type discriminator，形成 EventData union。
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from core.schema.agent import AgentState, EventType
from core.schema.types import MessageId, RequestId, SessionId
from Module.workflow_contract.schema import WorkflowId


class VoiceReason(StrEnum):
    """SystemTickData 的触发原因枚举。"""
    SILENCE_TIMEOUT = "silence_timeout"
    TIME_OF_DAY = "time_of_day"


class InterruptData(BaseModel):
    event_type: Literal[EventType.INTERRUPT] = EventType.INTERRUPT
    source: str = Field(default="?")


class StateChangeData(BaseModel):
    event_type: Literal[EventType.STATE_CHANGE] = EventType.STATE_CHANGE
    state: AgentState
    request_id: RequestId


class UserInputData(BaseModel):
    event_type: Literal[EventType.USER_INPUT] = EventType.USER_INPUT
    text: str
    user_name: str
    request_id: RequestId
    content: list[dict] | None = None
    emotion: str = ""
    events: list[str] = Field(default_factory=list)
    # 批次 2（v2 消息）：预分配用户消息 ID（ACK 一致）+ 来源协议版本
    message_id: MessageId | None = None
    protocol_version: int = 1
    # 批次 3（multi-session）：可选会话 ID，None = 未归属
    conversation_id: SessionId | None = None
    # 内部生命周期标记：True 表示 Agent/voice 已在 SessionManager.reserve_work()
    # 登记 accepted work；消费终态、丢弃或中断时必须成对 release_work()。
    session_work_reserved: bool = False


class SystemTickData(BaseModel):
    event_type: Literal[EventType.SYSTEM_TICK] = EventType.SYSTEM_TICK
    reason: VoiceReason
    elapsed: float | None = None
    time: str | None = None


class TaskResultData(BaseModel):
    event_type: Literal[EventType.TASK_RESULT] = EventType.TASK_RESULT
    task_id: WorkflowId
    result: str
    original_intent: str
    dispatched_at: str
    is_error: bool = False


EventData = Annotated[
    InterruptData | StateChangeData | UserInputData | SystemTickData | TaskResultData,
    Field(discriminator="event_type"),
]
