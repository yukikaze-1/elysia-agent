"""GenerationContext — 一次 v2 生成的分段/增量状态追踪（批次 2）。

每回合创建一个：持有 generation_id / 用户消息 ID / assistant 父消息 ID，
维护全局分段序号、段内 delta 序号。loop 发射统一回合事件时据此填充 v2 wire ID。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.schema.types import GenerationId, MessageId, RequestId, SegmentId, SessionId
from core.utils.message_id import new_segment_id


@dataclass
class GenerationContext:
    """一次 AI 生成的上下文状态。

    Attributes:
        generation_id: 生成实例 ID（gen_ 前缀）。
        user_message_id: 触发本次生成的用户消息 ID（ACK 预分配，落库一致）。
        assistant_message_id: 本次生成产出的 assistant 父消息 ID（generation 开始预分配，落库用）。
        conversation_id: 本次生成归属的会话 ID（批次 3 multi-session，经 loop process 贯通）。
        model_id: 本次生成实际使用模型（批次 5 D8：turn 启动时由 loop 捕获 llm.model 快照，
            统一回合事件 Started/Completed/Failed 的 model 数据源——替代 completed 即时读取）。
        request_id: 本次生成关联的 WS 侧请求 ID（批次 7：来源 UserInputData.request_id，
            贯通 GenerationRegistry 建档与关联排查；无则 None）。
        segment_index: 下一个分段将使用的全局序号（跨 inner_voice/public_reply/工具轮单调递增）。
        delta_sequence: 当前段内已分配的 delta 序号计数。
        current_segment_id: 当前打开分段的 ID（无活动分段为 None）。
        current_segment_index: 当前分段的全局序号（无活动分段为 None）。
        channel / kind: 当前分段通道/类型（无活动分段为 None）。
        protocol_version: 来源协议版本（1=v1，2=v2）。批次 8b D5：空响应兜底按
            协议版本分支——v2 发 segment 帧，v1 保持 MessageObj。默认 1（既有构造兼容）。
    """

    generation_id: GenerationId
    user_message_id: MessageId
    assistant_message_id: MessageId
    conversation_id: SessionId | None = None
    model_id: str = ""
    request_id: RequestId | None = None
    protocol_version: int = 1
    segment_index: int = 0
    delta_sequence: int = 0
    current_segment_id: SegmentId | None = None
    current_segment_index: int | None = None
    channel: str | None = None
    kind: str | None = None

    def open_segment(self, channel: str, kind: str = "text") -> SegmentId:
        """打开一个新分段：分配 seg_ ID、全局序号递增、重置段内 delta。

        Args:
            channel: 分段通道（inner_voice/public_reply/status）。
            kind: 分段类型（P0 仅 text/markdown）。

        Returns:
            新分段的 segment_id。
        """
        self.current_segment_id = new_segment_id()
        self.current_segment_index = self.segment_index
        self.segment_index += 1
        self.delta_sequence = 0
        self.channel = channel
        self.kind = kind
        return self.current_segment_id

    def next_delta(self) -> int:
        """返回当前段内下一个 delta 序号（从 0 递增）。"""
        seq = self.delta_sequence
        self.delta_sequence += 1
        return seq

    def close_segment(self) -> None:
        """关闭当前分段（清空活动分段状态）。"""
        self.current_segment_id = None
        self.current_segment_index = None
        self.channel = None
        self.kind = None
