"""输出通道抽象接口。每个通道声明能力，按需 override 方法。"""

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any


class ChannelCapability(StrEnum):
    """通道能力标记。outputManager 据此过滤目标通道。"""
    TEXT_DELTA = "text_delta"
    TEXT_MESSAGE = "text_message"
    TYPING = "typing"
    EXPRESSION = "expression"
    ACTION = "action"
    AUDIO = "audio"
    COMMAND = "command"


class OutputChannel(ABC):
    """输出通道抽象基类。

    所有方法默认 no-op，通道只 override 自己关心的。
    capabilities + on_message + close 为 abstract，必须实现。
    """

    # 协商的协议版本：1=v1（默认），2=v2。连接握手时由 WebSocketChannel 设置。
    protocol_version: int = 1

    @property
    @abstractmethod
    def capabilities(self) -> set[ChannelCapability]:
        """本通道支持的能力集合。"""
        ...

    async def on_typing(self, typing: bool) -> None:
        """正在输入... / 输入结束

        Args:
            typing: True 表示开始输入，False 表示输入结束。
        """

    async def on_text_delta(self, delta: str) -> None:
        """流式文本增量 — 逐 token 推送

        Args:
            delta: 单个文本增量（token）。
        """

    async def on_expression(self, expression: str, intensity: float = 1.0) -> None:
        """表情/情绪变更 — 前端可驱动 Live2D / 头像动画

        Args:
            expression: 表情/情绪标识。
            intensity: 强度，0.0~1.0。
        """

    async def on_action(self, action: str, *, params: dict | None = None) -> None:
        """动作变更 — 如 wave, nod, surprised + 参数

        Args:
            action: 动作标识。
            params: 可选的动作参数。
        """

    async def on_tool_started(self, *, call_id: str, tool_name: str, runtime: str,
                              input_summary: str | None = None,
                              generation_id: str | None = None,
                              conversation_id: str | None = None) -> None:
        """工具开始（v2 展示生命周期）：默认 no-op，通道按需 override。"""

    async def on_tool_completed(self, *, call_id: str, tool_name: str, runtime: str,
                                success: bool, output_summary: str | None = None,
                                error: dict | None = None, error_type: str | None = None,
                                duration_ms: float | None = None,
                                truncated: bool | None = None,
                                generation_id: str | None = None,
                                conversation_id: str | None = None) -> None:
        """工具完成（v2 展示生命周期）：默认 no-op，通道按需 override。"""

    async def on_audio_chunk(self, chunk: bytes, *, stream_id: int = 0) -> None:
        """音频流 chunk — stream_id 区分并发流

        Args:
            chunk: 音频字节数据。
            stream_id: 流标识，用于区分并发音频流。
        """

    async def on_audio_end(self, stream_id: int = 0, *, format: str = "wav") -> None:
        """音频流结束标记 — 前端可据此 finalize 播放。

        Args:
            stream_id: 流标识，用于区分并发音频流。
            format: 音频格式，'wav' 或 'mp3'。
        """

    async def on_audio_start(self, stream_id: int, *, format: str = "wav",
                             sample_rate: int | None = None,
                             duration_ms: int | None = None) -> None:
        """音频流开始（BE-063）。默认空实现。"""

    async def on_audio_cancelled(self, stream_id: int) -> None:
        """音频流被取消（BE-063）。默认空实现。"""

    async def on_command(self, cmd: str, *, params: dict | None = None) -> None:
        """前端指令 — 换背景、播放特效、弹出图片

        Args:
            cmd: 指令标识。
            params: 可选的指令参数。
        """

    async def on_state(self, state: str) -> None:
        """FSM 状态变更 — 前端据此切换动画（待机/思考/说话）

        Args:
            state: FSM 状态名。
        """

    async def on_token_usage(self, data: dict) -> None:
        """Token 消耗统计 — 前端展示用量条形图/标签

        Args:
            data: token 消耗统计数据字典。
        """

    # ── 批次 2 v2 结构化事件（协议版本 2 通道按需实现；默认 no-op） ──
    # 批次 3：全部 6 类 generation.* 加 conversation_id（会话归属，no-op 默认不变）。

    async def on_user_message_ack(self, **kwargs: Any) -> None:
        """v2 用户消息确认（wire user_message.ack）。"""

    async def on_generation_started(self, *, conversation_id: str | None = None,
                                    **kwargs: Any) -> None:
        """v2 生成开始（wire generation.started）。"""

    async def on_generation_segment_started(self, *, conversation_id: str | None = None,
                                            **kwargs: Any) -> None:
        """v2 分段开始（wire generation.segment.started）。"""

    async def on_generation_delta(self, *, conversation_id: str | None = None,
                                  **kwargs: Any) -> None:
        """v2 分段增量（wire generation.delta）。"""

    async def on_generation_segment_completed(self, *, conversation_id: str | None = None,
                                              **kwargs: Any) -> None:
        """v2 分段完成（wire generation.segment.completed）。"""

    async def on_generation_completed(self, *, conversation_id: str | None = None,
                                      conversation_usage: dict | None = None,
                                      **kwargs: Any) -> None:
        """v2 父完成（wire generation.completed）。

        conversation_usage: 批次 3 会话级累计 dict {total, by_model}。
        """

    async def on_generation_failed(self, *, conversation_id: str | None = None,
                                   **kwargs: Any) -> None:
        """v2 父失败（wire generation.failed）。"""

    @abstractmethod
    async def on_message(self, msg: Any) -> None:
        """完整消息 — 流式结束后发送，或非流式消息直接发送。

        Args:
            msg: 消息对象（ChatMessage 或 str）。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭通道"""
        ...
