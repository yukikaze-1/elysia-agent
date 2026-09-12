"""DebugInfo — 聚合 Agent 内部状态，供 /debug 仪表盘使用。

用法:
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
    # 系统
    fsm_state: str = "idle"
    fsm_previous: str | None = None
    turn_phase: str | None = None          # CognitiveLoop 内部处理阶段
    processing: bool = False
    uptime_seconds: float = 0.0           # 服务运行时长

    # IdleMonitor（空闲监听）
    proactive_enabled: bool = False
    proactive_interval: float = 0.0
    last_interaction_ago: float = 0.0     # 距上次用户交互的秒数

    # 会话
    session_messages: int = 0
    session_recent: list[dict] = field(default_factory=list)
    character_state_raw: dict = field(default_factory=dict)
    archive_msg_count: int = 0            # 归档消息总数

    # 长期记忆
    lt_count: int = 0
    lt_recent: list[dict] = field(default_factory=list)
    milvus_ok: bool = False
    embedding_ok: bool = False
    mongodb_ok: bool = False

    # 工具
    currently_executing: list[str] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    tool_history: list[dict] = field(default_factory=list)

    # 记忆沉淀（Consolidator）
    consolidation_history: list[dict] = field(default_factory=list)

    # 性格反思（CharacterReflector）
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

    # 角色
    character_name: str = ""
    base_personality_summary: str = ""

    # CharacterEngine（4 层模型，合并 PersonalityEngine + EmotionEngine）
    character_engine_exists: bool = False
    self_l2: str = ""              # L2 Self — 长期性格演化
    state_l3: str = ""             # L3 State — 短期状态
    state_history: list[dict] = field(default_factory=list)  # L3 历史
    last_awareness_time: float = 0.0  # C9 上次触发时间戳
    l2_version_count: int = 0       # L2 版本历史总数
    c6_version_count: int = 0       # C6 版本历史总数

    # 连接
    tts_ok: bool = False
    stt_ok: bool = False
    ws_clients: int = 0

    # LLM（大语言模型）
    llm_model: str = ""

    # Token 统计
    token_stats: dict = field(default_factory=dict)
    pending_token_count: int = 0
    pending_session_exists: bool = False

    # Prompt 记录
    prompt_summary: dict = field(default_factory=dict)
    response_summary: dict = field(default_factory=dict)

    # 延迟（环形缓冲区——最近 100 次请求）
    timing_history: list[dict] = field(default_factory=list)
    timing_stats: dict = field(default_factory=dict)

    # MemoryReflector（记忆去重）
    mreflector_exists: bool = False
    mreflector_round_count: int = 0
    mreflector_round_threshold: int = 0
    mreflector_new_memory_count: int = 0
    mreflector_memory_threshold: int = 0
    mreflector_locked: bool = False
    mreflector_last_summary: dict = field(default_factory=dict)

    # 沉淀器缓冲
    consolidator_buffer_size: int = 0
    consolidator_threshold: int = 0

    # 心情（L4 情绪）
    mood: str = ""

    # 记忆
    memory: dict = field(default_factory=dict)

    # 调度器工作线程池
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


def _collect_system(agent: "Agent", info: DebugInfo) -> None:
    """收集 System / IdleMonitor 面板数据。"""
    info.fsm_state = agent.status.current_name()
    info.fsm_previous = agent.status.previous_name()
    phase = agent.cognitive._phase  # noqa: SLF001
    info.turn_phase = phase.value if phase else None
    ct = agent.cognitive._current_task  # noqa: SLF001
    info.processing = ct is not None and not ct.done()
    info.uptime_seconds = time.monotonic() - agent.started_at

    im = agent.idle
    info.proactive_enabled = im._character.proactive.enabled  # noqa: SLF001
    info.proactive_interval = im._character.proactive.interval  # noqa: SLF001
    if im._last_interaction:  # noqa: SLF001
        info.last_interaction_ago = time.monotonic() - im._last_interaction  # noqa: SLF001


async def _collect_session_and_memory(agent: "Agent", info: DebugInfo) -> None:
    """收集 Session / 长期记忆 / 数据库健康面板数据。"""
    msgs = agent.session.messages_for_client()
    info.session_messages = len(msgs)
    try:
        info.archive_msg_count = await agent.message_repo.count()
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
    # Task 12（§6.5 读端迁移）：character_state_raw 读当前 instance engine.state
    # （实例权威）。此前读 session.character_state——M3 后 state 权威在 runtime.state，
    # session.character_state 不再被写（stale）。
    info.character_state_raw = agent.character_engine.state.model_dump(mode="json")

    info.lt_count = agent.long_term.count
    info.lt_recent = [
        {
            "id": e.id,
            "text": _truncate(e.text, 100),
            "memory_type": e.memory_type,
            "poignancy": e.poignancy,
            "metadata": e.metadata,
        }
        for e in agent.long_term._entries[-5:]  # noqa: SLF001
    ]
    info.milvus_ok = await agent.memory_repo.health_check()
    info.embedding_ok = agent.long_term._embedding_provider._backend is not None  # noqa: SLF001
    info.mongodb_ok = await agent.session_repo.health_check()


def _collect_tools_and_consolidation(agent: "Agent", info: DebugInfo) -> None:
    """收集 Tools / Consolidation 面板数据。"""
    tm = agent.tool_adapter  # 公开门面（批次 A：原 `._manager` 私有穿透）
    info.currently_executing = list(tm.executing_now)
    info.tools = tm.plugin_list
    info.tool_history = list(tm.history)[-5:]

    # Task 7（记忆 v2 隔离）：agent.consolidator 现经 registry 解析（可能 None——无 default
    # runtime 或未预置时）。None 时清空历史（不抛）。
    cons = agent.consolidator
    info.consolidation_history = (
        list(cons._history)[-5:] if cons else [])  # noqa: SLF001


async def _collect_character_and_reflector(agent: "Agent", info: DebugInfo) -> None:
    """收集 Character / CharacterEngine / Reflector 面板数据。"""
    info.character_name = agent.character.name
    bp = agent.character.base_personality
    info.base_personality_summary = bp[:120] + "..." if len(bp) > 120 else bp

    pe = agent.character_engine
    info.character_engine_exists = True
    c1 = pe.render_c1()
    info.self_l2 = c1.self_personality[:200] if c1.self_personality else "（未演化）"
    l3 = pe.state.state_l3
    info.state_l3 = l3[:200] if l3 else "（无短期状态）"
    info.state_history = [dict(e) for e in pe.state.state_history[-5:]]
    info.last_awareness_time = pe.state.last_awareness_time or 0.0

    try:
        info.l2_version_count = 0
        engine = getattr(agent, "character_engine", None)
        if engine and engine.version_repo:
            sid = agent.session.session_id
            info.l2_version_count = await engine.version_repo.count_versions(
                sid, content_type="l2")
    except Exception:
        info.l2_version_count = 0

    try:
        info.c6_version_count = 0
        engine = getattr(agent, "character_engine", None)
        if engine and engine.version_repo:
            info.c6_version_count = await engine.version_repo.count_versions(
                agent.session.session_id, content_type="c6")
    except Exception:
        info.c6_version_count = 0

    ref = agent.character_reflector
    # `character_reflector` 的类型是 `CharacterReflectorProtocol | None`（引擎缺失时
    # 为 None）。面板宁可展示「不存在」也不该 500——探针降级原则同 health。
    info.reflector_exists = ref is not None
    if ref is not None:
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


def _collect_runtime_stats(agent: "Agent", info: DebugInfo) -> None:
    """收集 Connections/LLM/Latency/Scheduler/MemoryReflector/Token/Recorder 面板数据。"""
    info.tts_ok = agent.tts is not None
    info.stt_ok = agent.stt_ok
    info.ws_clients = sum(
        1 for ch in agent.output.channels
        if type(ch).__name__ == "WebSocketChannel"
    )

    info.llm_model = agent.llm.model or "?"

    info.timing_history = list(agent.cognitive._timing_history)  # noqa: SLF001
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

    try:
        sched = agent.scheduler
        info.scheduler_pending = sched.pending_count
        info.scheduler_running = sched.running_count
        info.scheduler_total = sched.total_count
        for t in sched.all_tasks():
            if t.type.value == "tool_call" and t.status.value in ("pending", "dispatched"):
                info.task_tree = sched.get_tree(t.id)
                break
    except Exception:
        pass

    # Task 7（记忆 v2 隔离）：agent.memory_reflector/consolidator 现经 registry 解析
    # （可能 None——无 default runtime 或未预置时）。None 时记 0 / 空，不抛。
    mr = agent.memory_reflector
    info.mreflector_exists = mr is not None
    info.mreflector_round_count = mr._round_count if mr else 0  # noqa: SLF001
    info.mreflector_round_threshold = mr.ROUND_THRESHOLD if mr else 0
    info.mreflector_new_memory_count = mr._new_memory_count if mr else 0  # noqa: SLF001
    info.mreflector_memory_threshold = mr.NEW_MEMORY_THRESHOLD if mr else 0
    info.mreflector_locked = mr._lock.locked() if mr else False  # noqa: SLF001
    info.mreflector_last_summary = (dict(mr._last_summary) if (mr and mr._last_summary) else {})  # noqa: SLF001

    cons = agent.consolidator
    info.consolidator_buffer_size = len(cons._buffer) if cons else 0  # noqa: SLF001
    info.consolidator_threshold = cons._threshold if cons else 0  # noqa: SLF001

    try:
        # Task 12（§6.5 读端迁移）：mood 读当前 instance engine.state（实例权威）。
        info.mood = agent.character_engine.state.mood
    except Exception:
        pass

    proc = psutil.Process(os.getpid())
    mem = proc.memory_info()
    info.memory = {
        "rss_mb": round(mem.rss / 1024 / 1024, 1),
        "vms_mb": round(mem.vms / 1024 / 1024, 1),
        "pct": round(proc.memory_percent(), 1),
        "num_threads": proc.num_threads(),
    }

    tracker = agent.token_tracker
    if tracker is not None:
        info.token_stats = {
            "session_total": tracker.session_total,
            "per_model": dict(tracker.per_model),
            "current_turn": [
                u.model_dump(mode="json")
                for u in tracker.current_turn_usages[-10:]
            ],
        }
        token_pending = str(ProjectPath.TOKEN_PENDING.resolve())
        if os.path.exists(token_pending):
            with open(token_pending) as f:
                info.pending_token_count = sum(1 for _ in f)
        info.pending_session_exists = os.path.exists(str(ProjectPath.SESSION_PENDING_CHECK.resolve()))

    recorder = agent.prompt_recorder
    if recorder is not None:
        info.prompt_summary = recorder.list_summaries()

    resp_recorder = agent.response_recorder
    if resp_recorder is not None:
        info.response_summary = resp_recorder.list_summaries()


async def collect(agent: "Agent") -> DebugInfo:
    """收集 Agent 实例的所有调试数据。

    Args:
        agent: 要收集调试数据的 Agent 实例。

    Returns:
        包含完整调试数据的 DebugInfo 对象。
    """
    info = DebugInfo()
    _collect_system(agent, info)
    await _collect_session_and_memory(agent, info)
    _collect_tools_and_consolidation(agent, info)
    await _collect_character_and_reflector(agent, info)
    _collect_runtime_stats(agent, info)
    return info
