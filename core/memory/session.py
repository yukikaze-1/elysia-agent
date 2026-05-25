"""Session context memory — in-memory with optional MongoDB persistence.

Stores the recent conversation window and runtime parameters.
Survives process restart if MongoDB is configured; otherwise ephemeral.
"""

from collections import deque
from typing import Any


class SessionMemory:
    """In-memory session context with a bounded message window."""

    def __init__(self, max_messages: int = 20):
        self._messages: deque[dict[str, Any]] = deque(maxlen=max_messages)
        self._params: dict[str, Any] = {}
        self._max_messages = max_messages

    # ---- messages ----

    def add_message(self, role: str, content: str, **meta) -> None:
        entry = {"role": role, "content": content}
        if meta:
            entry["meta"] = meta
        self._messages.append(entry)

    def recent_messages(self, n: int | None = None) -> list[dict[str, Any]]:
        """Return the most recent N messages (default: all)."""
        msgs = list(self._messages)
        if n is not None:
            msgs = msgs[-n:]
        return msgs

    # ---- runtime params ----

    def set_param(self, key: str, value: Any) -> None:
        self._params[key] = value

    def get_param(self, key: str, default: Any = None) -> Any:
        return self._params.get(key, default)

    # ---- lifecycle ----

    def clear(self) -> None:
        self._messages.clear()
        self._params.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "messages": list(self._messages),
            "params": dict(self._params),
        }

    def restore(self, data: dict[str, Any]) -> None:
        self._messages = deque(data.get("messages", [])[-self._max_messages:], maxlen=self._max_messages)
        self._params = dict(data.get("params", {}))
