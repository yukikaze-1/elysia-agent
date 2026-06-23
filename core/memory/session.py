"""Session context — in-memory with optional MongoDB persistence.

Auto-detects MongoDB availability. When available, sessions survive
process restarts via save_session() / load_session().
Otherwise falls back to in-memory deque (existing behavior).
"""

import json
import logging
import os
from collections import deque
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class SessionMemory:
    """Bounded conversation window with optional MongoDB persistence."""

    def __init__(self, max_messages: int = 20, session_id: str = "default"):
        self._messages: deque[dict[str, Any]] = deque(maxlen=max_messages)
        self._params: dict[str, Any] = {}
        self._max_messages = max_messages
        self._session_id = session_id
        self._db: Any = None  # DocumentDBBackend or None
        self._pending_file = os.path.join("data", f"session_{session_id}.json")

    # ---- public properties (替代直接访问私有属性) ----

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def db(self) -> Any:
        return self._db

    @property
    def messages(self) -> deque[dict[str, Any]]:
        return self._messages

    @property
    def pending_file(self) -> str:
        return self._pending_file

    # ---- messages ----

    def add_message(self, *args, **meta) -> None:
        """存储一条消息。兼容新旧格式。

        旧格式: add_message(role, content, **meta)       — content 为 str
        新格式: add_message({role, content, message_id, ...}) — content 为 list[dict] (ChatMessageV2.to_dict())
        """
        if len(args) == 1 and isinstance(args[0], dict):
            msg = args[0]
            # 向后兼容：content 为 str 时包装为 TextPart 数组
            if isinstance(msg.get("content"), str):
                msg["content"] = [{"type": "text", "content": msg["content"]}]
            self._messages.append(msg)
        else:
            role, content = args[0], args[1] if len(args) > 1 else ""
            entry: dict[str, Any] = {
                "role": role,
                "content": [{"type": "text", "content": content}],
            }
            if meta:
                entry["meta"] = meta
            self._messages.append(entry)

    def recent_messages(self, n: int | None = None) -> list[dict[str, Any]]:
        msgs = list(self._messages)
        if n is not None:
            msgs = msgs[-n:]
        return msgs

    def messages_for_client(self, n: int | None = None) -> list[dict[str, Any]]:
        """返回客户端友好的消息格式。旧格式消息自动转换为 parts + content 数组。"""
        msgs = self.recent_messages(n)
        result = []
        for m in msgs:
            role = m.get("role", "")

            # 工具结果消息 — 透传完整字段给前端渲染工具卡片
            if role == "tool":
                tool_content = m.get("content", "")
                # add_message 将 str content 包装成了 TextPart 数组，提取回纯文本
                if isinstance(tool_content, list):
                    tool_content = "".join(
                        p.get("content", "") for p in tool_content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                entry: dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "name": m.get("name", ""),
                    "content": tool_content,
                    "success": m.get("success", False),
                    "error": m.get("error"),
                    "error_type": m.get("error_type"),
                    "duration_ms": m.get("duration_ms", 0),
                    "truncated": m.get("truncated", False),
                }
                result.append(entry)
                continue

            if "parts" in m:
                entry = dict(m)
                # 保留 tool_calls / inner_voice
                if "tool_calls" in m:
                    entry["tool_calls"] = m["tool_calls"]
                if "inner_voice" in m:
                    entry["inner_voice"] = m["inner_voice"]
                result.append(entry)
            else:
                entry: dict[str, Any] = {"role": role}
                # 保留 tool_calls / inner_voice
                if "tool_calls" in m:
                    entry["tool_calls"] = m["tool_calls"]
                if "inner_voice" in m:
                    entry["inner_voice"] = m["inner_voice"]
                content = m.get("content", "")
                # 旧格式 meta.sentences 优先（add_message 旧调用路径）
                meta = m.get("meta", {})
                if "sentences" in meta:
                    entry["parts"] = [{"content": s} for s in meta["sentences"]]
                elif isinstance(content, list):
                    # ChatMessageV2 格式 — content 是 Part 数组
                    # 提取 text parts 作为 parts 数组传给前端
                    text_parts = [
                        {"content": p["content"]}
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    entry["parts"] = text_parts if text_parts else [{"content": ""}]
                else:
                    entry["parts"] = [{"content": str(content)}] if content else [{"content": ""}]

                if isinstance(content, list):
                    entry["content"] = content  # 保留完整 content 给前端渲染图片
                elif isinstance(content, str):
                    entry["content"] = [{"type": "text", "content": content}]
                else:
                    entry["content"] = [{"type": "text", "content": ""}]
                entry["content_type"] = m.get("content_type", {})
                if "token_usage" in m:
                    entry["token_usage"] = m["token_usage"]
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
        try:
            from Module.database.documentdb import DocumentDBFactory
            self._db = DocumentDBFactory.create("mongodb/default")
            await self._db.ensure_index("sessions", [("session_id", 1)])
            await self._db.ensure_index("sessions", [("updated_at", -1)])
            # Try TTL index for auto-cleanup of stale sessions (30 days)
            try:
                await self._db.ensure_index(
                    "sessions",
                    [("updated_at", 1)],
                    expireAfterSeconds=30 * 86400,
                )
            except Exception:
                pass  # TTL index not supported by all MongoDB-compatible backends
            logger.info("SessionMemory: MongoDB connected")
            return True
        except Exception as e:
            logger.info("SessionMemory: MongoDB unavailable (%s), using in-memory", e)
            self._db = None
            return False

    async def save(self) -> bool:
        """Persist current session。MongoDB 不可用时降级到本地 JSONL 文件。"""
        doc = {
            "session_id": self._session_id,
            "messages": list(self._messages),
            "params": dict(self._params),
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        if self._db is None:
            try:
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
        try:
            logger.info("Session save to MongoDB: session=%s, messages=%d",
                        self._session_id, len(doc["messages"]))
            await self._db.update_one(
                "sessions",
                {"session_id": self._session_id},
                {"$set": doc},
                upsert=True,
            )
            logger.info("Session save to MongoDB OK: %d messages", len(doc["messages"]))
            return True
        except Exception:
            logger.warning("session MongoDB 写入失败", exc_info=True)
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
            msgs = doc.get("messages", [])
            if merge and self._messages:
                # 合并：现有消息（from MongoDB）+ JSONL 消息，按 id 去重
                existing_ids = {m.get("id") for m in self._messages if m.get("id")}
                new_msgs = [m for m in msgs if m.get("id") not in existing_ids]
                all_msgs = list(self._messages) + new_msgs
                self._messages = deque(all_msgs[-self._max_messages:], maxlen=self._max_messages)
                logger.info("从 %s 合并 session（已有 %d + 新增 %d）",
                           self._pending_file, len(self._messages) - len(new_msgs), len(new_msgs))
            else:
                self._messages = deque(msgs[-self._max_messages:], maxlen=self._max_messages)
                logger.info("从 %s 恢复 session（%d 条消息）", self._pending_file, len(msgs))
            self._params = dict(doc.get("params", {}))
            return True
        except Exception:
            logger.warning("从 %s 恢复 session 失败", self._pending_file, exc_info=True)
            return False

    async def load(self, session_id: str | None = None) -> bool:
        """Load session from MongoDB。MongoDB 不可用时尝试本地 JSONL 恢复。"""
        if self._db is None:
            return await self._load_from_jsonl()
        sid = session_id or self._session_id
        try:
            results = await self._db.find("sessions", {"session_id": sid}, limit=1)
            if results:
                doc = results[0]
                self._session_id = sid
                msgs = doc.get("messages", [])
                self._messages = deque(msgs[-self._max_messages:], maxlen=self._max_messages)
                self._params = dict(doc.get("params", {}))
                logger.info("SessionMemory: loaded session %s (%d messages)", sid, len(msgs))
                return True
            return False
        except Exception:
            logger.warning("SessionMemory load from MongoDB failed, trying JSONL", exc_info=True)
            return await self._load_from_jsonl()

    def snapshot(self) -> dict[str, Any]:
        return {"messages": list(self._messages), "params": dict(self._params)}

    def restore(self, data: dict[str, Any]) -> None:
        self._messages = deque(data.get("messages", [])[-self._max_messages:], maxlen=self._max_messages)
        self._params = dict(data.get("params", {}))

    def clear(self) -> None:
        self._messages.clear()
        self._params.clear()
