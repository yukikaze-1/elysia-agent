"""Agent 共享类型 — 零包内依赖，消除循环导入。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.workflow_engine.mailbox import WorkflowMailbox


@dataclass
class _PendingMessage:
    """FIFO 队列中暂存的消息（FSM 忙碌时入队）。"""
    text: str
    user_name: str
    request_id: str
    content: list[dict] | None = None
    # 批次 2（v2 消息）：预分配用户消息 ID——忙时排队出队后 ACK 与落库保持一致
    message_id: str | None = None
    # 批次 3（multi-session）：可选会话 ID，None = 未归属（loop 路由消费）
    conversation_id: str | None = None
    # review 修复（F3/链路#1）：来源协议版本——忙时排队出队重建 UserInputData 时必须
    # 透传，否则 v2 排队消息出队后降级为 v1 语义（恒 default），丢失会话归属。
    protocol_version: int = 1


@dataclass
class _BackgroundTaskEntry:
    """后台任务注册信息。"""
    intent: str
    dispatched_at: str
    mailbox: WorkflowMailbox


class RunState(Enum):
    """Agent 生命周期状态"""
    STOPPED = auto()      # 初始 / 已停止
    RUNNING = auto()      # 运行中
    STOPPING = auto()     # 正常关机中
    RESTARTING = auto()   # 重启中
