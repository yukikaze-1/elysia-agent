"""DebugInfo — aggregates internal agent state for the /debug dashboard.

Usage:
    info = await collect(agent)
    return web.json_response(asdict(info))
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, asdict
from typing import TYPE_CHECKING

import psutil
from paths import ProjectPath

if TYPE_CHECKING:
    from core.agent import Agent


@dataclass
class DebugInfo:
    # System
    fsm_state: str = "idle"
    fsm_previous: str | None = None
    turn_phase: str | None = None          # CognitiveLoop 内部处理阶段
    processing: bool = False
    uptime_seconds: float = 0.0           # 服务运行时长

    # IdleMonitor
    proactive_enabled: bool = False
    proactive_interval: float = 0.0
    last_interaction_ago: float = 0.0     # 距上次用户交互的秒数

    # Session
    session_messages: int = 0
    session_recent: list[dict] = field(default_factory=list)
    character_state_raw: dict = field(default_factory=dict)
    archive_msg_count: int = 0            # 归档消息总数

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

    # CharacterEngine (4-layer model, merged PersonalityEngine + EmotionEngine)
    character_engine_exists: bool = False
    self_l2: str = ""              # L2 Self — 长期性格演化
    state_l3: str = ""             # L3 State — 短期状态
    state_history: list[dict] = field(default_factory=list)  # L3 历史
    last_awareness_time: float = 0.0  # C9 上次触发时间戳
    l2_version_count: int = 0       # L2 版本历史总数
    c6_version_count: int = 0       # C6 版本历史总数

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

    # MemoryReflector
    mreflector_exists: bool = False
    mreflector_round_count: int = 0
    mreflector_round_threshold: int = 0
    mreflector_new_memory_count: int = 0
    mreflector_memory_threshold: int = 0
    mreflector_locked: bool = False
    mreflector_last_summary: dict = field(default_factory=dict)

    # Consolidator buffer
    consolidator_buffer_size: int = 0
    consolidator_threshold: int = 0

    # Mood (L4 Emotion)
    mood: str = ""

    # Memory
    memory: dict = field(default_factory=dict)

    # Scheduler Worker Pool
    scheduler_pending: int = 0
    scheduler_running: int = 0
    scheduler_total: int = 0
    task_tree: dict = field(default_factory=dict)

def _truncate(text: str, n: int = 80) -> str:
    """截断文本到指定长度，超出部分用省略号替代。

    Args:
        text: 待截断的文本。
        n: 最大字符数，默认 80。

    Returns:
        截断后的文本字符串。
    """
    return text if len(text) <= n else text[:n] + "…"


async def collect(agent: "Agent") -> DebugInfo:
    """收集 Agent 实例的所有调试数据。

    Args:
        agent: 要收集调试数据的 Agent 实例。

    Returns:
        包含完整调试数据的 DebugInfo 对象。
    """
    info = DebugInfo()

    # ---- System ----
    info.fsm_state = agent.status.current_name()
    info.fsm_previous = agent.status.previous_name()
    phase = agent._cognitive._phase
    info.turn_phase = phase.value if phase else None
    ct = agent._cognitive._current_task
    info.processing = ct is not None and not ct.done()
    # uptime
    info.uptime_seconds = time.monotonic() - agent._started_at

    # ---- IdleMonitor ----
    im = agent._idle
    info.proactive_enabled = im._character.proactive.enabled
    info.proactive_interval = im._character.proactive.interval
    if im._last_interaction:
        info.last_interaction_ago = time.monotonic() - im._last_interaction

    # ---- Session ----
    msgs = agent._session.messages_for_client()
    info.session_messages = len(msgs)
    # archive count — 直调 message_repo
    try:
        info.archive_msg_count = await agent._message_repo.count()
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
    info.character_state_raw = agent._session.character_state.model_dump(mode="json")

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
    repo = agent._long_term._repo
    info.milvus_ok = await agent._memory_repo.health_check()
    info.embedding_ok = agent._long_term._embedding_provider._backend is not None
    info.mongodb_ok = await agent._session_repo.health_check()

    # ---- Tools ----
    tm = agent._tool_adapter._manager
    info.currently_executing = list(tm.executing_now)
    info.tools = tm.plugin_list
    info.tool_history = list(tm.history)[-5:]

    # ---- Consolidation ----
    info.consolidation_history = list(agent._consolidator._history)[-5:]

    # ---- Character ----
    info.character_name = agent._character.name
    bp = agent._character.base_personality
    info.base_personality_summary = bp[:120] + "..." if len(bp) > 120 else bp

    # ---- CharacterEngine ----
    # ---- CharacterEngine ----
    pe = agent._character_engine
    info.character_engine_exists = True
    c1 = pe.render_c1()
    info.self_l2 = c1.self_personality[:200] if c1.self_personality else "（未演化）"
    l3 = pe.state.state_l3
    info.state_l3 = l3[:200] if l3 else "（无短期状态）"
    info.state_history = [dict(e) for e in pe.state.state_history[-5:]]
    info.last_awareness_time = pe.state.last_awareness_time or 0.0

    # ---- L2/C6 Version History ----
    try:
        info.l2_version_count = 0
        engine = getattr(agent, "_character_engine", None)
        if engine and getattr(engine, "_version_repo", None):
            sid = agent._session.session_id
            info.l2_version_count = await engine._version_repo.count_versions(
                sid, content_type="l2")
    except Exception:
        info.l2_version_count = 0

    try:
        info.c6_version_count = 0
        engine = getattr(agent, "_character_engine", None)
        if engine and getattr(engine, "_version_repo", None):
            info.c6_version_count = await engine._version_repo.count_versions(
                agent._session.session_id, content_type="c6")
    except Exception:
        info.c6_version_count = 0

    # ---- Character Reflector ----
    ref = agent._character_reflector
    info.reflector_exists = True
    info.reflector_buffer_size = ref.buffer_size
    info.reflector_last_reflection_at = ref.last_reflection_at
    info.reflector_locked = ref.locked
    info.reflector_last_prompt_system = ref.last_prompt_system[:1000]
    info.reflector_last_prompt_user = ref.last_prompt_user[:1000]
    info.reflector_last_success = ref.last_success
    info.reflector_last_duration_ms = ref.last_duration_ms
    info.reflector_last_text_len = ref.last_text_len

    history = pe.state.self_perception_history
    info.self_perception_history = list(history[-5:])

    # ---- Connections ----
    info.tts_ok = agent._tts is not None
    info.stt_ok = agent._stt_ok
    info.ws_clients = sum(
        1 for ch in agent._output._channels
        if type(ch).__name__ == "WebSocketChannel"
    )

    # ---- LLM ----
    info.llm_model = agent._llm.model or "?"

    # ---- Latency ----
    info.timing_history = list(agent._cognitive._timing_history)
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
        sched = agent._scheduler
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

    # ---- MemoryReflector ----
    mr = agent._memory_reflector
    info.mreflector_exists = True
    info.mreflector_round_count = mr._round_count
    info.mreflector_round_threshold = mr.ROUND_THRESHOLD
    info.mreflector_new_memory_count = mr._new_memory_count
    info.mreflector_memory_threshold = mr.NEW_MEMORY_THRESHOLD
    info.mreflector_locked = mr._lock.locked()
    info.mreflector_last_summary = dict(mr._last_summary) if mr._last_summary else {}

    # ---- Consolidator buffer ----
    cons = agent._consolidator
    info.consolidator_buffer_size = len(cons._buffer)
    info.consolidator_threshold = cons._threshold

    # ---- Mood (L4 Emotion) ----
    try:
        info.mood = agent._session.character_state.mood
    except Exception:
        pass

    # ---- Process Memory ----
    proc = psutil.Process(os.getpid())
    mem = proc.memory_info()
    info.memory = {
        "rss_mb": round(mem.rss / 1024 / 1024, 1),
        "vms_mb": round(mem.vms / 1024 / 1024, 1),
        "pct": round(proc.memory_percent(), 1),
        "num_threads": proc.num_threads(),
    }

    # ---- Token 统计 ----
    tracker = agent._token_tracker
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
        token_pending = str(ProjectPath.TOKEN_PENDING.resolve())
        if os.path.exists(token_pending):
            with open(token_pending) as f:
                info.pending_token_count = sum(1 for _ in f)
        info.pending_session_exists = os.path.exists(str(ProjectPath.SESSION_PENDING_CHECK.resolve()))

    # ---- Prompt 记录 ----
    recorder = agent._prompt_recorder
    if recorder is not None:
        info.prompt_summary = recorder.list_summaries()

    # ---- Response 记录 ----
    resp_recorder = agent._response_recorder
    if resp_recorder is not None:
        info.response_summary = resp_recorder.list_summaries()

    return info
