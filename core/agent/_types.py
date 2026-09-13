"""Agent 共享类型 — 零包内依赖，消除循环导入。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.schema.types import MessageId, RequestId, SessionId
from core.status.protocol import RunState as _RunState

if TYPE_CHECKING:
    from Module.workflow_engine.mailbox import WorkflowMailbox


@dataclass
class _PendingMessage:
    """FIFO 队列中暂存的消息（FSM 忙碌时入队）。"""
    text: str
    user_name: str
    request_id: RequestId
    content: list[dict] | None = None
    emotion: str = ""
    events: list[str] | None = None
    # 批次 2（v2 消息）：预分配用户消息 ID——忙时排队出队后 ACK 与落库保持一致
    message_id: MessageId | None = None
    # 批次 3（multi-session）：可选会话 ID，None = 未归属（loop 路由消费）
    conversation_id: SessionId | None = None
    # review 修复（F3/链路#1）：来源协议版本——忙时排队出队重建 UserInputData 时必须
    # 透传，否则 v2 排队消息出队后降级为 v1 语义（恒 default），丢失会话归属。
    protocol_version: int = 1
    # v2 accepted work 已预占目标会话；出队后透传给 UserInputData，终态释放。
    session_work_reserved: bool = False
    # CR-13：发起本次消息的 WS 连接 ID（v2 握手分配；v1/REST 为 ""）。HITL 高危
    # 工具审批据此绑定"只有发起连接能批准"——否则多客户端下 B 可从广播帧里直接
    # 拿到 request_id 批准 A 的高危调用。
    origin_connection_id: str = ""


@dataclass
class _BackgroundTaskEntry:
    """后台任务注册信息。"""
    intent: str
    dispatched_at: str
    mailbox: WorkflowMailbox


# RunState 已移至 `core.status.protocol`（#43：它是 AgentStatus 接口的一部分，
# 放接口层才能让状态层持有它而不反向依赖 core.agent）。此处 re-export 兼容既有
# 导入路径（`from core.agent._types import RunState` / `from core.agent import RunState`）。
RunState = _RunState
