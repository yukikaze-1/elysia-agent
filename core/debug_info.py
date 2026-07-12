"""DebugInfo — aggregates internal agent state for the /debug dashboard.

Usage:
    info = await collect(agent)
    return web.json_response(asdict(info))
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict


@dataclass
class DebugInfo:
    # System
    fsm_state: str = "idle"
    fsm_previous: str | None = None
    processing: bool = False
    uptime_seconds: float = 0.0           # 服务运行时长

    # IdleMonitor
    proactive_enabled: bool = False
    proactive_interval: float = 0.0
    last_interaction_ago: float = 0.0     # 距上次用户交互的秒数

    # Session
    session_messages: int = 0
    session_recent: list[dict] = field(default_factory=list)
    session_params: dict = field(default_factory=dict)
    archive_msg_count: int = 0            # MessageArchive 总数

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

    # Character Reflector
    reflector_exists: bool = False
    reflector_buffer_size: int = 0
    reflector_last_reflection_at: float | None = None
    reflector_locked: bool = False
    self_perception_history: list[dict] = field(default_factory=list)
    reflector_last_prompt_system: str = ""
    reflector_last_prompt_user: str = ""
    reflector_last_success: bool = False
    reflector_last_duration_ms: float = 0.0
    reflector_last_text_len: int = 0

    # Character
    character_name: str = ""
    base_personality_summary: str = ""

    # PersonalityEngine (4-layer model)
    personality_engine_exists: bool = False
    self_l2: str = ""              # L2 Self — 长期性格演化
    state_l3: str = ""             # L3 State — 短期状态
    state_history: list[dict] = field(default_factory=list)  # L3 历史
    last_awareness_time: float = 0.0  # C9 上次触发时间戳

    # Connections
    tts_ok: bool = False
    stt_ok: bool = False
    ws_clients: int = 0

    # LLM
    llm_model: str = ""

    # Token 统计
    token_stats: dict = field(default_factory=dict)
    pending_token_count: int = 0
    pending_session_exists: bool = False

    # Prompt 记录
    prompt_summary: dict = field(default_factory=dict)
    response_summary: dict = field(default_factory=dict)

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
    # uptime
    started = getattr(agent, '_started_at', None)
    if started is not None:
        info.uptime_seconds = time.monotonic() - started

    # ---- IdleMonitor ----
    im = getattr(agent, '_idle_monitor', None)
    if im is not None:
        info.proactive_enabled = getattr(im._character, 'proactive', None) is not None and im._character.proactive.enabled
        info.proactive_interval = getattr(im._character.proactive, 'interval', 0.0) if hasattr(im, '_character') else 0.0
        last_touch = getattr(im, '_last_interaction', 0.0)
        if last_touch:
            info.last_interaction_ago = time.monotonic() - last_touch

    # ---- Session ----
    msgs = agent._session.messages_for_client()
    info.session_messages = len(msgs)
    # archive count
    archive = getattr(agent._session, '_archive', None)
    if archive:
        try:
            info.archive_msg_count = await archive.count()
        except Exception:
            pass
    info.session_recent = [
        {
            "role": m.get("role", "?"),
            "parts": m.get("parts", [{"content": ""}]),
            "content": m.get("content", []),
            "content_type": m.get("content_type", {}),
        }
        for m in msgs[-5:]
    ]
    info.session_params = dict(agent._session._params)

    # ---- Long-term memory ----
    info.lt_count = agent._long_term.count
    info.lt_recent = [
        {
            "id": e.id,
            "text": _truncate(e.text, 100),
            "memory_type": e.memory_type,
            "poignancy": e.poignancy,
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
    bp = agent._character.base_personality
    info.base_personality_summary = bp[:120] + "..." if len(bp) > 120 else bp

    # ---- PersonalityEngine ----
    pe = getattr(agent, "_personality_engine", None)
    if pe is not None:
        info.personality_engine_exists = True
        c1 = pe.render_c1()
        info.self_l2 = c1["self_personality"][:200] if c1["self_personality"] else "（未演化）"
        l3 = agent._session.get_param("state_l3", "")
        info.state_l3 = l3[:200] if l3 else "（无短期状态）"
        info.state_history = list(agent._session.get_param("state_history", []))[-5:]
        info.last_awareness_time = agent._session.get_param("last_awareness_time", 0.0)

    # ---- Character Reflector ----
    ref = getattr(agent, "_character_reflector", None)
    if ref is not None:
        info.reflector_exists = True
        info.reflector_buffer_size = ref.buffer_size
        info.reflector_last_reflection_at = ref.last_reflection_at
        info.reflector_locked = ref.locked
        info.reflector_last_prompt_system = ref.last_prompt_system[:1000]
        info.reflector_last_prompt_user = ref.last_prompt_user[:1000]
        info.reflector_last_success = ref.last_success
        info.reflector_last_duration_ms = ref.last_duration_ms
        info.reflector_last_text_len = ref.last_text_len

    history = agent._session.get_param("self_perception_history", [])
    info.self_perception_history = list(history[-5:])

    # ---- Connections ----
    info.tts_ok = agent._tts is not None
    info.stt_ok = getattr(agent, '_stt_ok', False)
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

    # ---- Token 统计 ----
    tracker = getattr(agent, "_token_tracker", None)
    if tracker is not None:
        info.token_stats = {
            "session_total": tracker.session_total,
            "per_model": dict(tracker.per_model),
            "current_turn": [
                {
                    "model": u.model,
                    "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "total_tokens": u.total_tokens,
                    "round_num": u.round_num,
                }
                for u in tracker.current_turn_usages[-10:]
            ],
        }
        # Pending 文件状态
        import os as _os2
        token_pending = "data/token_usage_pending.jsonl"
        if _os2.path.exists(token_pending):
            with open(token_pending) as f:
                info.pending_token_count = sum(1 for _ in f)
        info.pending_session_exists = _os2.path.exists("data/session_pending.jsonl")

    # ---- Prompt 记录 ----
    recorder = getattr(agent, '_prompt_recorder', None)
    if recorder is not None:
        info.prompt_summary = recorder.list_summaries()

    # ---- Response 记录 ----
    resp_recorder = getattr(agent, '_response_recorder', None)
    if resp_recorder is not None:
        info.response_summary = resp_recorder.list_summaries()

    return info
