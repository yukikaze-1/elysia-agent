"""Debug routes 共享 helper。

内部 `core.schema.Message` → 契约视图转换：`sessions_to_client` 输出
`frontend_contract.debug.SessionDetailMessage` wire 形状（role/timestamp/content_type/parts/content），
由发送点（archive.py session_detail_handler）逐条 `model_validate` 构造契约模型。
"""

from core.schema.message import Message


def sessions_to_client(msgs: list[Message] | list[dict]) -> list[dict]:
    """将消息列表转为 session 详情前端兼容格式（SessionDetailMessage 契约视图）。

    接受 Message 或原始 dict，输出 role/timestamp/content_type/parts/content 形状，
    供 `SessionDetailMessage.model_validate` 构造契约模型（发送点转换）。

    Args:
        msgs: Message 对象列表或原始 dict 列表。

    Returns:
        list[dict]: 前端兼容的消息字典列表，每项包含 role、timestamp、parts 等字段。
    """
    result = []
    for m in msgs:
        if isinstance(m, Message):
            entry = m.model_dump(mode="json")
        else:
            entry = dict(m)
        item: dict = {"role": entry.get("role", "?"),
                      "timestamp": entry.get("timestamp", ""),
                      "content_type": entry.get("content_type", {})}
        content = entry.get("content", "")
        if isinstance(content, list):
            item["parts"] = [
                {"content": p.get("content", "") if isinstance(p, dict) else getattr(p, "content", "")}
                for p in content
                if (isinstance(p, dict) and p.get("type") == "text")
                   or (not isinstance(p, dict) and getattr(p, "type", None) == "text")
            ]
            item["content"] = content
        elif isinstance(content, str):
            item["parts"] = [{"content": content}]
        elif "parts" in entry:
            item["parts"] = entry["parts"]
        else:
            item["parts"] = [{"content": str(content)}]
        result.append(item)
    return result
