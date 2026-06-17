"""文本工具 — 从 content 数组提取纯文本 + 轻量消息对象。"""

from dataclasses import dataclass


def content_text_from_content(content: list[dict]) -> str:
    """从 content 数组提取纯文本（给日志用）。"""
    return "".join(
        p.get("content", "") for p in content
        if isinstance(p, dict) and p.get("type") == "text"
    )


@dataclass
class MessageObj:
    """轻量 assistant 消息对象 — 非流式文本最终发送。"""

    role: str = "assistant"
    content: str = ""

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}
