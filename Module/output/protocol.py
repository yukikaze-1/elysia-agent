"""输出通道抽象接口。每个通道声明能力，按需 override 方法。"""

from abc import ABC, abstractmethod
from enum import StrEnum


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

    @property
    @abstractmethod
    def capabilities(self) -> set[ChannelCapability]:
        """本通道支持的能力集合。"""
        ...

    async def on_typing(self, typing: bool) -> None:
        """正在输入... / 输入结束"""

    async def on_text_delta(self, delta: str) -> None:
        """流式文本增量 — 逐 token 推送"""

    async def on_expression(self, expression: str, intensity: float = 1.0) -> None:
        """表情/情绪变更 — 前端可驱动 Live2D / 头像动画"""

    async def on_action(self, action: str, *, params: dict | None = None) -> None:
        """动作变更 — 如 wave, nod, surprised + 参数"""

    async def on_audio_chunk(self, chunk: bytes, *, stream_id: int = 0) -> None:
        """音频流 chunk — stream_id 区分并发流"""

    async def on_audio_end(self, stream_id: int = 0) -> None:
        """音频流结束标记 — 前端可据此 finalize 播放"""

    async def on_command(self, cmd: str, *, params: dict | None = None) -> None:
        """前端指令 — 换背景、播放特效、弹出图片"""

    async def on_state(self, state: str) -> None:
        """FSM 状态变更 — 前端据此切换动画（待机/思考/说话）"""

    @abstractmethod
    async def on_message(self, msg) -> None:
        """完整消息 — 流式结束后发送，或非流式消息直接发送。"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭通道"""
        ...
