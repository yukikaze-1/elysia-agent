"""Session context — in-memory with optional MongoDB persistence.

Auto-detects MongoDB availability. When available, sessions survive
process restarts via save_session() / load_session().
Otherwise falls back to in-memory deque (existing behavior).
"""

import asyncio
import json
import logging
import os
from collections import deque
from typing import Any

from pydantic import TypeAdapter

from core.schema.message import (
    AssistantMessage,
    ClientAssistantMessage,
    ClientMessageDict,
    ClientToolMessage,
    ClientUserMessage,
    Message,
    ToolResultMessage,
)
from core.schema._utils import now_iso
from core.schema.part import TextPart
from core.schema.types import SessionId
from core.storage.message_repo import MessageRepository
from core.storage.session_repo import SessionRepository

logger = logging.getLogger(__name__)

# TypeAdapter for Message union — cached at module level
_message_adapter: TypeAdapter[Message] = TypeAdapter(Message)


def _archive_done(task: asyncio.Task) -> None:
    """fire-and-forget 归档回调：静默吞掉异常（归档为 best-effort）。"""
    try:
        task.result()
    except Exception:
        logger.debug("消息归档失败（fire-and-forget）", exc_info=True)


class SessionMemory:
    """Bounded conversation window with optional MongoDB persistence."""

    def __init__(self, max_messages: int = 20,
                 session_id: SessionId = SessionId("default"),
                 repo: SessionRepository | None = None,
                 msg_repo: MessageRepository | None = None):
        self._messages: deque[Message] = deque(maxlen=max_messages)
        self._params: dict[str, Any] = {}
        self._max_messages = max_messages
        self._session_id: SessionId = session_id
        self._repo: SessionRepository | None = repo
        self._msg_repo: MessageRepository | None = msg_repo
        self._pending_file = os.path.join("data", f"session_{session_id}.json")

    # ---- public properties ----

    @property
    def session_id(self) -> SessionId:
        return self._session_id

    @property
    def messages(self) -> deque[Message]:
        return self._messages

    @property
    def pending_file(self) -> str:
        return self._pending_file

    # ---- messages ----

    def add_message(self, msg: Message) -> None:
        """存储一条 Pydantic Message 对象，同时 fire-and-forget 归档到永久存储。"""
        self._messages.append(msg)
        if self._msg_repo is not None:
            task = asyncio.ensure_future(
                self._msg_repo.append(self._session_id, msg)
            )
            task.add_done_callback(_archive_done)

    def recent_messages(self, n: int | None = None) -> list[Message]:
        """返回最近 n 条消息（Pydantic 对象）。n 为 None 时返回全部。"""
        msgs = list(self._messages)
        if n is not None:
            msgs = msgs[-n:]
        return msgs

    def messages_for_client(self, n: int | None = None) -> list[ClientMessageDict]:
        """返回客户端友好的消息格式，从 Pydantic Message 转换为前端 dict。"""
        msgs = self.recent_messages(n)
        result: list[ClientMessageDict] = []
        for msg in msgs:
            if isinstance(msg, ToolResultMessage):
                # 工具结果 — 平铺字段给前端渲染工具卡片
                text_content = "".join(
                    p.content for p in msg.content
                    if isinstance(p, TextPart)
                )
                entry: ClientToolMessage = {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "name": msg.tool_name,
                    "content": text_content,
                    "success": msg.success,
                    "error": msg.error,
                    "error_type": msg.error_type.value if msg.error_type else None,
                    "duration_ms": msg.duration_ms,
                    "truncated": msg.truncated,
                }
                result.append(entry)
            else:
                # UserMessage / AssistantMessage
                text_parts = [
                    {"content": p.content}
                    for p in msg.content
                    if isinstance(p, TextPart)
                ]
                if isinstance(msg, AssistantMessage):
                    entry: ClientAssistantMessage = {
                        "role": str(msg.role),
                        "parts": text_parts if text_parts else [{"content": ""}],
                        "content": [p.model_dump(mode="json") for p in msg.content],
                        "message_id": msg.message_id,
                        "timestamp": msg.timestamp,
                    }
                    if msg.inner_voice:
                        entry["inner_voice"] = msg.inner_voice
                    if msg.tool_calls:
                        entry["tool_calls"] = [tc.model_dump(mode="json") for tc in msg.tool_calls]
                    if msg.token_usage:
                        entry["token_usage"] = msg.token_usage.model_dump(mode="json")
                    # block_index → content_type（debug 页面 / 前端旧字段名）
                    if msg.block_index:
                        entry["content_type"] = msg.block_index  # type: ignore[typeddict-item]
                else:
                    entry: ClientUserMessage = {
                        "role": str(msg.role),
                        "parts": text_parts if text_parts else [{"content": ""}],
                        "content": [p.model_dump(mode="json") for p in msg.content],
                        "message_id": msg.message_id,
                        "timestamp": msg.timestamp,
                    }
                result.append(entry)
        return result

    # ---- runtime params ----

    def set_param(self, key: str, value: Any) -> None:
        self._params[key] = value

    def get_param(self, key: str, default: Any = None) -> Any:
        return self._params.get(key, default)

    # ---- MongoDB persistence ----

    async def connect_db(self) -> bool:
        """Try to connect to MongoDB. Returns True if successful."""
        if self._repo is None:
            self._repo = SessionRepository()
        return await self._repo.connect()

    async def health_check(self) -> bool:
        """尝试重连 MongoDB。已连接则直接返回 True。"""
        if self._repo is None:
            return False
        return await self._repo.health_check()

    async def save(self) -> bool:
        """Persist current session。MongoDB 不可用时降级到本地 JSONL 文件。"""
        # 尝试通过 Repository 保存到 MongoDB
        if self._repo is not None:
            try:
                logger.info("Session save to MongoDB: session=%s, messages=%d",
                            self._session_id, len(self._messages))
                await self._repo.save(
                    self._session_id,
                    list(self._messages),
                    dict(self._params),
                )
                logger.info("Session save to MongoDB OK: %d messages", len(self._messages))
                return True
            except Exception:
                logger.warning("session MongoDB 写入失败", exc_info=True)
        # JSONL fallback — MongoDB 不可用
        try:
            doc = {
                "session_id": self._session_id,
                "messages": [msg.model_dump(mode="json") for msg in self._messages],
                "params": dict(self._params),
                "updated_at": now_iso(),
            }
            os.makedirs(os.path.dirname(self._pending_file), exist_ok=True)
            tmp = self._pending_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(doc, ensure_ascii=False) + "\n")
            os.replace(tmp, self._pending_file)  # 原子替换，崩溃不丢数据
            logger.debug("MongoDB 不可用，session 保存到 %s", self._pending_file)
            return True
        except Exception:
            logger.warning("session JSON 写入失败", exc_info=True)
            return False

    async def load_from_jsonl(self, merge: bool = False) -> bool:
        """公开接口——从本地 JSONL 文件恢复 session。"""
        return await self._load_from_jsonl(merge=merge)

    async def _load_from_jsonl(self, merge: bool = False) -> bool:
        """从本地 JSONL 文件恢复 session。

        merge=False: 直接替换 _messages（用于 MongoDB 不可用时的独立恢复）
        merge=True:  合并到现有 _messages 后面（用于 MongoDB 恢复后补充 JSONL 数据）
        """
        if not os.path.exists(self._pending_file):
            return False
        try:
            with open(self._pending_file) as f:
                doc = json.loads(f.readline())
            msgs_raw = doc.get("messages", [])
            converted = [_message_adapter.validate_python(m) for m in msgs_raw]
            if merge and self._messages:
                # 合并：现有消息（from MongoDB）+ JSONL 消息，按 message_id 去重
                existing_ids = {m.message_id for m in self._messages if m.message_id}
                new_msgs = [m for m in converted if m.message_id not in existing_ids]
                all_msgs = list(self._messages) + new_msgs
                self._messages = deque(all_msgs[-self._max_messages:], maxlen=self._max_messages)
                logger.info("从 %s 合并 session（已有 %d + 新增 %d）",
                           self._pending_file, len(self._messages) - len(new_msgs), len(new_msgs))
            else:
                self._messages = deque(converted[-self._max_messages:], maxlen=self._max_messages)
                logger.info("从 %s 恢复 session（%d 条消息）", self._pending_file, len(converted))
            self._params = dict(doc.get("params", {}))
            return True
        except Exception:
            logger.warning("从 %s 恢复 session 失败", self._pending_file, exc_info=True)
            return False

    async def load(self, session_id: SessionId | None = None) -> bool:
        """Load session from MongoDB。MongoDB 不可用时尝试本地 JSONL 恢复。"""
        if self._repo is None:
            return await self._load_from_jsonl()
        sid = session_id or self._session_id
        try:
            result = await self._repo.load(sid)
            if result is not None:
                msgs_raw, params = result
                self._session_id = sid
                # 从 dict 转换回 Message 对象
                converted = [_message_adapter.validate_python(m) for m in msgs_raw]
                self._messages = deque(converted[-self._max_messages:], maxlen=self._max_messages)
                self._params = dict(params)
                logger.info("SessionMemory: loaded session %s (%d messages)", sid, len(converted))
                return True
            return await self._load_from_jsonl()
        except Exception:
            logger.warning("SessionMemory load from MongoDB failed, trying JSONL", exc_info=True)
            return await self._load_from_jsonl()

    def clear(self) -> None:
        self._messages.clear()
        self._params.clear()
