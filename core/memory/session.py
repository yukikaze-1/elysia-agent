"""Session context — in-memory with optional MongoDB persistence.

Auto-detects MongoDB availability. When available, sessions survive
process restarts via save_session() / load_session().
Otherwise falls back to in-memory deque (existing behavior).
"""

import logging
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
            if "parts" in m:
                result.append(m)
            else:
                entry: dict[str, Any] = {"role": m["role"]}
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
                    entry["parts"] = [{"content": ""}]

                if isinstance(content, list):
                    entry["content"] = content  # 保留完整 content 给前端渲染图片
                elif isinstance(content, str):
                    entry["content"] = [{"type": "text", "content": content}]
                else:
                    entry["content"] = [{"type": "text", "content": ""}]
                entry["content_type"] = m.get("content_type", {})
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
        """Persist current session to MongoDB (atomically upsert). No-op if DB unavailable."""
        if self._db is None:
            return False
        try:
            doc = {
                "session_id": self._session_id,
                "messages": list(self._messages),
                "params": dict(self._params),
                "updated_at": datetime.now().isoformat(),
            }
            await self._db.update_one(
                "sessions",
                {"session_id": self._session_id},
                {"$set": doc},
                upsert=True,
            )
            return True
        except Exception as e:
            logger.warning("SessionMemory save failed: %s", e)
            return False

    async def load(self, session_id: str | None = None) -> bool:
        """Load session from MongoDB. Returns True if session was found."""
        if self._db is None:
            return False
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
        except Exception as e:
            logger.warning("SessionMemory load failed: %s", e)
            return False

    def snapshot(self) -> dict[str, Any]:
        return {"messages": list(self._messages), "params": dict(self._params)}

    def restore(self, data: dict[str, Any]) -> None:
        self._messages = deque(data.get("messages", [])[-self._max_messages:], maxlen=self._max_messages)
        self._params = dict(data.get("params", {}))

    def clear(self) -> None:
        self._messages.clear()
        self._params.clear()
