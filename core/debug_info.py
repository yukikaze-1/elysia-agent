"""DebugInfo — aggregates internal agent state for the /debug dashboard.

Usage:
    info = await collect(agent)
    return web.json_response(asdict(info))
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class DebugInfo:
    # System
    fsm_state: str = "idle"
    fsm_previous: str | None = None
    processing: bool = False

    # Session
    session_messages: int = 0
    session_recent: list[dict] = field(default_factory=list)
    session_params: dict = field(default_factory=dict)

    # Long-term memory
    lt_count: int = 0
    lt_recent: list[dict] = field(default_factory=list)
    milvus_ok: bool = False
    embedding_ok: bool = False
    mongodb_ok: bool = False

    # Tools
    tools: list[str] = field(default_factory=list)
    tool_history: list[dict] = field(default_factory=list)

    # Consolidation
    consolidation_history: list[dict] = field(default_factory=list)

    # Character
    character_name: str = ""
    character_traits: list[str] = field(default_factory=list)

    # Connections
    tts_ok: bool = False
    ws_clients: int = 0

    # LLM
    llm_model: str = ""


def _truncate(text: str, n: int = 80) -> str:
    return text if len(text) <= n else text[:n] + "…"


async def collect(agent) -> DebugInfo:
    """Gather all debug data from a DemoAgent instance."""
    info = DebugInfo()

    # ---- System ----
    info.fsm_state = str(agent._fsm.state)
    info.fsm_previous = str(agent._fsm.previous) if agent._fsm.previous else None
    ct = agent._cognitive._current_task
    info.processing = ct is not None and not ct.done()

    # ---- Session ----
    msgs = agent._session.recent_messages()
    info.session_messages = len(msgs)
    info.session_recent = [
        {
            "role": m.get("role", "?"),
            "content": _truncate(m.get("content", ""), 100),
        }
        for m in msgs[-5:]
    ]
    info.session_params = dict(agent._session._params)

    # ---- Long-term memory ----
    info.lt_count = agent._long_term.count
    info.lt_recent = [
        {
            "text": _truncate(e.text, 100),
            "metadata": e.metadata,
        }
        for e in agent._long_term._entries[-5:]
    ]
    info.milvus_ok = agent._long_term._connected
    info.embedding_ok = agent._long_term._embedding is not None
    info.mongodb_ok = agent._session._db is not None

    # ---- Tools ----
    info.tools = list(agent._tools.tool_names)
    info.tool_history = list(getattr(agent._tools, "_history", []))[-5:]

    # ---- Consolidation ----
    info.consolidation_history = list(getattr(agent._consolidator, "_history", []))[-5:]

    # ---- Character ----
    info.character_name = agent._character.name
    info.character_traits = list(agent._character.traits)

    # ---- Connections ----
    info.tts_ok = agent._tts is not None
    info.ws_clients = sum(
        1 for ch in agent._output._channels
        if type(ch).__name__ == "WebSocketChannel"
    )

    # ---- LLM ----
    info.llm_model = getattr(getattr(agent, "_llm", None), "model", "") or "?"

    return info
