"""WS 消息契约：ack 域（ARCH-INC-P2-02 物理拆分）。

装配点见 `frontend_contract/ws_message/__init__.py`；该文件是唯一的
`ServerMessage`/`ClientMessage` 联合定义位置。
"""
from __future__ import annotations


from frontend_contract.base import ContractModel
from pydantic import Discriminator, Field
from typing import Annotated, Any, Literal, Optional, Union
from frontend_contract.ws_message._types import AcceptedUserAckMessage, RejectedUserAckMessage


# 模块级命名判别联合（Python 侧类型用；ServerMessage 的 ack 分支引用它，生成器递归展开）
UserAckMessage = Annotated[
    Union[AcceptedUserAckMessage, RejectedUserAckMessage],
    Discriminator("status"),
]



class ErrorMessage(ContractModel):
    """错误消息。

    requestId（批次 7，P7-12）：关联 WS 侧请求 ID（来源 UserInputData.request_id，
    有则带、无则 None 缺省省略）——客户端可凭此关联日志/幂等排查。
    clientRequestId（批次 B，BE-P1-03）：关联客户端请求 ID——命令类消息限流时提取
    回显，前端可凭此 settle 对应 promise。
    retryAfterMs（批次 B）：顶层建议重试退避毫秒（RATE_LIMITED 时），可缺省。
    """

    type: Literal["error"] = "error"
    message: str = Field(description="错误描述")
    code: Optional[str] = Field(default=None, description="错误码（如 SESSION_NOT_FOUND），可缺省")
    requestId: Optional[str] = Field(default=None, description="关联请求 ID（WS 侧 request_id），可缺省")
    clientRequestId: Optional[str] = Field(
        default=None, description="关联客户端请求 ID（命令类限流提取回显），可缺省")
    retryable: Optional[bool] = Field(default=None, description="是否可重试，可缺省")
    retryAfterMs: Optional[int] = Field(
        default=None, description="建议重试退避毫秒（RATE_LIMITED 时），可缺省")
    details: Optional[dict[str, Any]] = Field(default=None, description="公开安全详情，可缺省")
