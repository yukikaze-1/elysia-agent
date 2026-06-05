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
    currently_executing: list[str] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
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

    # Latency (ring buffer — last 100 requests)
    timing_history: list[dict] = field(default_factory=list)
    timing_stats: dict = field(default_factory=dict)

    # Memory
    memory: dict = field(default_factory=dict)

    # Scheduler Worker Pool
    scheduler_pending: int = 0
    scheduler_running: int = 0
    scheduler_total: int = 0
    task_tree: dict = field(default_factory=dict)

def _truncate(text: str, n: int = 80) -> str:
    return text if len(text) <= n else text[:n] + "…"


async def collect(agent) -> DebugInfo:
    """Gather all debug data from a Agent instance."""
    info = DebugInfo()

    # ---- System ----
    info.fsm_state = str(agent._fsm.state)
    info.fsm_previous = str(agent._fsm.previous) if agent._fsm.previous else None
    ct = agent._cognitive._current_task
    info.processing = ct is not None and not ct.done()

    # ---- Session ----
    msgs = agent._session.messages_for_client()
    info.session_messages = len(msgs)
    info.session_recent = [
        {
            "role": m.get("role", "?"),
            "parts": m.get("parts", [{"content": ""}]),
            "content_type": m.get("content_type", {}),
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
    tm = getattr(agent, "_tool_manager", None)
    if tm is not None:
        info.currently_executing = list(tm.executing_now)
        info.tools = tm.plugin_list
        info.tool_history = list(tm.history)[-5:]

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

    # ---- Latency ----
    info.timing_history = list(getattr(agent._cognitive, "_timing_history", []))
    if info.timing_history:
        totals = sorted(h["total_ms"] for h in info.timing_history)
        n = len(totals)
        info.timing_stats = {
            "count": n,
            "avg_ms": sum(totals) // n,
            "p50_ms": totals[n // 2],
            "p95_ms": totals[int(n * 0.95)],
            "p99_ms": totals[int(n * 0.99)] if n >= 100 else totals[-1],
            "last_ms": totals[-1],
        }

    # ---- Scheduler ----
    try:
        sched = getattr(agent, "_scheduler", None)
        if sched is not None:
            info.scheduler_pending = sched.pending_count
            info.scheduler_running = sched.running_count
            info.scheduler_total = sched.total_count
            # 找第一个活跃的工具子任务展示 task tree
            for t in sched.all_tasks():
                if t.type.value == "tool_call" and t.status.value in ("pending", "dispatched"):
                    info.task_tree = sched.get_tree(t.id)
                    break
    except Exception:
        pass

    # ---- Memory ----
    try:
        import os as _os
        import psutil
        proc = psutil.Process(_os.getpid())
        mem = proc.memory_info()
        info.memory = {
            "rss_mb": round(mem.rss / 1024 / 1024, 1),
            "vms_mb": round(mem.vms / 1024 / 1024, 1),
            "pct": round(proc.memory_percent(), 1),
            "num_threads": proc.num_threads(),
        }
    except Exception:
        info.memory = {"error": "psutil not available"}

    return info
