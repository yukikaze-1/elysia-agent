from enum import Enum

from pydantic import BaseModel, Field, computed_field, ConfigDict


class L0InputSourceType(str, Enum):
    WEBSOCKET = "websocket"
    INVALID = "invalid"


class WebClientMessage(BaseModel):
    role: str = Field(..., description="发送角色，如 'user'")
    content: str = Field(..., min_length=1, description="消息内容，不能为空")
    timestamp: float = Field(..., description="消息发送的时间戳")
    last_ai_timestamp: float = Field(..., description="上一条 AI 消息完成回复的时间戳")

    @computed_field
    @property
    def reaction_latency(self) -> float:
        return max(0.0, self.timestamp - self.last_ai_timestamp)


class TelegramMessage(BaseModel):
    chat_id: int
    text: str
    sender_username: str


class ExternalInputEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    source: L0InputSourceType = Field(..., description="数据来源通道，必须是合法枚举值")


class L0InternalQueueItem(BaseModel):
    source: L0InputSourceType = Field(..., description="消息来源")
    payload: WebClientMessage | TelegramMessage = Field(..., description="消息载荷对象")
