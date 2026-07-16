"""文本工具 — 从 content 数组提取纯文本 + 轻量消息对象。"""

from dataclasses import dataclass

from core.schema.message import MessageRole


def content_text_from_content(content: list[dict] | str | None) -> str:
    """从原始 content 提取纯文本（给日志和 prompt 渲染用）。

    处理三种格式：None → ""，str → 原样返回，list[dict] → 提取 text type 的 content。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(
        p.get("content", "") for p in content
        if isinstance(p, dict) and p.get("type") == "text"
    )


@dataclass
class MessageObj:
    """轻量 assistant 消息对象 — 非流式文本最终发送。"""

    role: MessageRole = MessageRole.ASSISTANT
    content: str = ""

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}
