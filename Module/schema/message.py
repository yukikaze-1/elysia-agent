from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import StrEnum
import logging
import time
import uuid
from typing import Optional

from openai.types.chat import ChatCompletionMessage

from Module.schema.io_models import L0InputSourceType
from Module.schema.media import AudioData, ImageData, VideoData, FileData


class MessageType(StrEnum):
    """ChatMessage 的内容类型"""
    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"
    FILE = "file"
    MIXED = "mixed"


# ============================================================
# UserMessage — 各输入端用户消息的统一抽象
#
# 来源:
#   CLI      — 命令行 stdin 直接输入
#   WEBSOCKET — Web 前端 / 客户端
#   TELEGRAM  — Telegram Bot (未来)
#
# 从输入端原始数据 (WebClientMessage / TelegramMessage / stdin) 规范化而来。
# 消费方: L0 → EventBus → UserInputHandler → L1 Brain → ChatMessage
# ============================================================

@dataclass
class UserMessage:
    """用户的一次主动交流行为"""

    content: str
    # 核心文本 — 键盘直接敲入、语音经 STT 转录、图片可为空或配 alt text

    source: L0InputSourceType
    # 入站通道，打标后不再改:
    #   L0InputSourceType.WEBSOCKET  — Web / 客户端
    #   L0InputSourceType.CLI        — 命令行 stdin (未来)
    #   L0InputSourceType.TELEGRAM   — Telegram Bot (未来)
    #   L0InputSourceType.INVALID    — 未识别来源，兜底

    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # 全链路追踪 ID，从入站到 ChatMessage 保持同一 ID

    session_id: str = ""
    # 多会话场景标识，CLI 可留空

    client_timestamp: float = field(default_factory=time.time)
    # 用户产生消息的物理时间，非系统收到时间

    attachments: list[FileData] = field(default_factory=list)
    # 用户附带文件/图片，CLI 为空。ChatMessage.from_UserMessage() 映射到 images/files 字段

    metadata: dict = field(default_factory=dict)
    # 各源特有数据，消费方按需读取:
    #   Web 客户端:
    #     "reaction_latency"  (float) — 用户反应延迟 (秒)
    #     "typing"            (dict)  — {"input_type", "typing_duration_ms", "delete_count"}
    #   CLI:
    #     "shell_cwd"         (str)   — 当前工作目录 (未来)
    #   麦克风 (STT 后):
    #     "audio_duration_ms" (int)   — 原始音频时长
    #     "audio_format"      (str)   — "wav" / "mp3"

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "source": self.source.value,
            "message_id": self.message_id,
            "session_id": self.session_id,
            "client_timestamp": self.client_timestamp,
            "attachments": [a.to_dict() if hasattr(a, "to_dict") else asdict(a) for a in self.attachments],
            "metadata": self.metadata,
        }

    def to_str(self) -> str:
        return (
            f"UserMessage(source={self.source.value}, content={self.content[:50]}, "
            f"id={self.message_id[:8]}..., session={self.session_id[:8]}...)"
        )


# ============================================================
# EnvironmentMessage — 环境事件输入的统一抽象
#
# 来源:
#   ACTIVE_SENSOR — 摄像头、麦克风、运动传感器等主动感知
#   SYSTEM        — 系统时钟、电量、网络状态等内部事件
#
# 从传感器原始输出规范化而来。
# 消费方: L0 → EventBus → Amygdala (情感反应) → L1 (决定是否主动提起)
# ============================================================

@dataclass
class EnvironmentMessage:
    """环境发生的一次状态变化"""

    event_type: str
    # 事件类型标识，自由字符串:
    #   摄像机:
    #     "object_fall"        — 物体掉落
    #     "person_enter"       — 有人进入视野
    #     "person_leave"       — 有人离开视野
    #     "motion_detected"    — 检测到运动
    #     "door_open"          — 门被打开
    #     "door_close"         — 门被关闭
    #   麦克风:
    #     "loud_sound"         — 检测到异常响声
    #     "voice_activity"     — 检测到人声 (非对话)
    #   系统:
    #     "time_period_change" — 时段变化 (清晨/上午/下午/夜晚)
    #     "power_low"          — 电量低
    #     "network_down"       — 网络断开
    #     "network_up"         — 网络恢复
    #     "system_startup"     — 系统启动
    #     "system_shutdown"    — 系统关闭

    source: L0InputSourceType
    # 事件来源:
    #   L0InputSourceType.ACTIVE_SENSOR  — 摄像头/麦克风等传感器
    #   L0InputSourceType.SYSTEM         — 系统内部事件 (时钟/电量等)

    payload: dict = field(default_factory=dict)
    # 结构化事件数据，按 event_type 约定 key:
    #   object_fall:      {"object": str, "location": str}
    #   person_enter:     {"count": int, "location": str}
    #   motion_detected:  {"location": str, "confidence": float}
    #   door_open/close:  {"door_id": str, "location": str}
    #   loud_sound:       {"level_db": float, "location": str}
    #   voice_activity:   {"duration_ms": int}
    #   time_period_change: {"from": str, "to": str}  # "morning" / "afternoon" / "evening" / "night"
    #   power_low:        {"level_percent": int}
    #   其他事件按需扩展

    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    session_id: str = ""

    timestamp: float = field(default_factory=time.time)
    # 事件发生时间

    metadata: dict = field(default_factory=dict)
    # 传感器特有数据，消费方按需读取

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "source": self.source.value,
            "payload": self.payload,
            "message_id": self.message_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    def to_str(self) -> str:
        return (
            f"EnvironmentMessage(type={self.event_type}, source={self.source.value}, "
            f"id={self.message_id[:8]}...)"
        )


# ============================================================
# ChatMessage — 系统内通用消息
#
# UserMessage 经 Handler 转换后成为 ChatMessage，在 SessionState、
# L1 Brain、L2 Memory、Actuator、Reflector 等所有层之间流通。
# ============================================================

@dataclass
class ChatMessage:
    role: str
    content: str
    inner_voice: str = ""
    timestamp: float = field(default_factory=time.time)
    type: MessageType = MessageType.TEXT
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    reply_to: str | None = None
    images: list[ImageData] = field(default_factory=list)
    files: list[FileData] = field(default_factory=list)
    audio: Optional[AudioData] = None
    video: Optional[VideoData] = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_multimodal(self) -> bool:
        return bool(self.images or self.files or self.audio or self.video)

    def to_dict(self) -> dict:
        return asdict(self)

    def debug(self, logger: logging.Logger):
        logger.info(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            role=data.get("role", ""),
            content=data.get("content", ""),
            inner_voice=data.get("inner_voice", ""),
            timestamp=data.get("timestamp", time.time()),
            type=MessageType(data.get("type", "text")),
            message_id=data.get("message_id", str(uuid.uuid4())),
            session_id=data.get("session_id", ""),
            reply_to=data.get("reply_to"),
            images=[ImageData(**img) if isinstance(img, dict) else img for img in data.get("images", [])],
            files=[FileData(**f) if isinstance(f, dict) else f for f in data.get("files", [])],
            audio=AudioData(**data["audio"]) if isinstance(data.get("audio"), dict) else data.get("audio"),
            video=VideoData(**data["video"]) if isinstance(data.get("video"), dict) else data.get("video"),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_ChatCompletionMessage(cls, message: ChatCompletionMessage, timestamp: int):
        return cls(
            role=message.role,
            content=message.content if message.content else "",
            inner_voice="",
            timestamp=float(timestamp),
            type=MessageType.TEXT,
        )

    @classmethod
    def from_UserMessage(cls, user_message: UserMessage):
        """从 UserMessage 构造 ChatMessage。

        UserMessage.attachments 按类型分流:
          - ImageData → images
          - FileData  → files
          - 其他      → 保留在 images/files (按 mime_type 自行判断)
        """
        images: list[ImageData] = []
        files: list[FileData] = []
        for a in user_message.attachments:
            if isinstance(a, ImageData):
                images.append(a)
            else:
                files.append(a)

        return cls(
            role=user_message.metadata.get("role", "user"),
            content=user_message.content,
            inner_voice="",
            timestamp=user_message.client_timestamp,
            message_id=user_message.message_id,
            session_id=user_message.session_id,
            images=images,
            files=files,
            metadata={
                **user_message.metadata,
                "source": user_message.source.value,
            },
        )


# ============================================================
# ConversationSegment — 对话片段
#
# MicroReflector 将 ChatMessage 列表按时间间隙切分成段。
# ============================================================

class ConversationSegment:
    def __init__(self, start_time: float, end_time: float, messages: list[ChatMessage]):
        self.messages: list[ChatMessage] = messages
        self.start_time: float = start_time
        self.end_time: float = end_time

    def format_messages_to_line(self):
        lines = []
        for msg in self.messages:
            lines.append(
                f"  {msg.role}: {msg.content}: {msg.timestamp}: "
                f"{datetime.fromtimestamp(msg.timestamp)}"
            )
        return "[\n" + "\n".join(lines) + "\n]"

    def debug(self, logger: logging.Logger):
        logger.info("Conversation Segment:")
        logger.info(
            f"During:{self.start_time} to {self.end_time}. "
            f"Contains {len(self.messages)} messages"
        )
        logger.info("Conversation Segment:" + self.format_messages_to_line())
