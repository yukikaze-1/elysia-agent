"""Debug API 契约建模：/debug/api 响应模型（零依赖，仅 pydantic + base）。

对照 `frontend/src/debug/types.ts` 的 17 接口 + `core/input/debug_routes/` 实际 wire 逐字段对齐：
- `DebugData`：DebugInfo dataclass 平铺字段 → 16 面板（System/Memory/Latency/.../CharacterEngine）。
- 命名策略：`ContentPart` → `DebugContentPart`、`TokenStats` → `DebugTokenStats`
  （这两个形状与 ws_message 不同——同名会触发 gen_ts 同名安全网 ValueError，按约束改名）。
- 契约视图：内部 core.schema 领域对象（Message/CompressionTrigger 等）不进本模块，
  wire 形状自建；发送点（debug_routes）把领域对象转成客户端 dict 后构造本模型。
- ts_null vs X?：字段按发送点实际 wire 定性——DebugInfo 恒写键（含 0/""）→ 必填/带默认值；
  仅在可缺省路径才建模为 Optional（X?，缺省省略）。
"""
from typing import Any, Optional

from pydantic import Field

from frontend_contract.base import ContractModel
from frontend_contract.rest import Message as ArchiveMessage

# ─────────────────────── 子结构（16 面板共享） ───────────────────────


class TokenRoundItem(ContractModel):
    """单轮 token 消耗（debug 面板 current_turn 条目）。"""

    model: str = Field(description="模型名")
    input_tokens: int = Field(description="输入 token 数")
    output_tokens: int = Field(description="输出 token 数")
    total_tokens: int = Field(description="总 token 数")
    round_num: int = Field(description="轮次序号")


class DebugTokenStats(ContractModel):
    """会话级 token 统计（debug 面板；与 ws_message.TokenStats 形状不同，按命名策略改名）。"""

    session_total: int = Field(default=0, description="会话累计 token 数")
    per_model: dict[str, int] = Field(default_factory=dict, description="按模型拆分的 token 数")
    current_turn: list[TokenRoundItem] = Field(default_factory=list, description="本轮各次调用消耗")


class TimingStats(ContractModel):
    """延迟聚合统计（ring buffer 前 100 条，空时各字段缺省）。"""

    count: Optional[int] = Field(default=None, description="样本数，可缺省")
    avg_ms: Optional[float] = Field(default=None, description="平均耗时毫秒，可缺省")
    p50_ms: Optional[float] = Field(default=None, description="P50 耗时毫秒，可缺省")
    p95_ms: Optional[float] = Field(default=None, description="P95 耗时毫秒，可缺省")
    p99_ms: Optional[float] = Field(default=None, description="P99 耗时毫秒，可缺省")
    last_ms: Optional[float] = Field(default=None, description="最近一次耗时毫秒，可缺省")


class ProcessMemory(ContractModel):
    """进程内存（rss/vms/pct/threads），不是长期记忆。"""

    rss_mb: float = Field(default=0.0, description="常驻内存 MB")
    vms_mb: float = Field(default=0.0, description="虚拟内存 MB")
    pct: float = Field(default=0.0, description="内存占用百分比")
    num_threads: int = Field(default=0, description="线程数")


class PromptRecord(ContractModel):
    """LLM 调用 prompt 摘要（PromptRecorder 输出）。"""

    index: int = Field(description="记录序号")
    timestamp: str = Field(description="ISO 时间戳")
    round_num: int = Field(description="轮次序号")
    message_count: int = Field(description="消息条数")
    system_prompt_len: int = Field(description="系统提示词长度")
    total_chars: int = Field(description="总字符数")
    model: str = Field(description="模型名")


class ResponseRecord(ContractModel):
    """LLM 调用原始回复摘要（ResponseRecorder 输出）。"""

    index: int = Field(description="记录序号")
    timestamp: str = Field(description="ISO 时间戳")
    round_num: int = Field(description="轮次序号")
    text_len: int = Field(description="回复文本长度")
    inner_voice_len: int = Field(description="内心独白长度")


class DebugContentPart(ContractModel):
    """内容块（text/image 统一形状；与 ws_message.ContentPart 判别联合不同，按命名策略改名）。"""

    type: str = Field(description="内容块类型（text/image）")
    content: Optional[str] = Field(default=None, description="文本内容，可缺省")
    src: Optional[str] = Field(default=None, description="图片来源（URL / data URI），可缺省")
    mime: Optional[str] = Field(default=None, description="MIME 类型，可缺省")


class DebugSessionPart(ContractModel):
    """session 消息中的纯文本 part（前端拼接展示）。"""

    content: str = Field(description="文本内容")


class SessionRecentItem(ContractModel):
    """最近会话消息（OverviewTab 面板 9）。"""

    role: str = Field(description="消息角色（user/assistant/tool）")
    parts: list[DebugSessionPart] = Field(default_factory=list, description="纯文本 parts 列表")
    content: list[DebugContentPart] = Field(default_factory=list, description="内容块列表")
    content_type: Optional[dict[str, Any]] = Field(
        default=None, description="内容类型 → 序号映射，可缺省")


class LtmRecentItem(ContractModel):
    """最近长期记忆（OverviewTab 面板 10）。"""

    id: str = Field(description="记忆 ID")
    text: str = Field(description="记忆文本（已截断）")
    memory_type: str = Field(description="记忆类型（fact/plan/preference/event/note）")
    poignancy: float = Field(description="重要性 1-10")
    metadata: Optional[dict[str, Any]] = Field(default=None, description="附加元数据，可缺省")


class ToolItem(ContractModel):
    """工具插件条目（OverviewTab 面板 11）。"""

    name: str = Field(description="工具名")
    description: str = Field(description="工具描述")
    parameters: Optional[dict[str, Any]] = Field(default=None, description="参数 schema，可缺省")


class ToolHistoryItem(ContractModel):
    """工具执行历史（OverviewTab 面板 12）。"""

    name: str = Field(description="工具名")
    args: Optional[Any] = Field(default=None, description="调用参数，可缺省")
    result: Optional[Any] = Field(default=None, description="执行结果，可缺省")
    error: Optional[str] = Field(default=None, description="错误信息，可缺省")
    success: Optional[bool] = Field(default=None, description="是否成功，可缺省")
    error_type: Optional[str] = Field(default=None, description="错误类型，可缺省")
    duration_ms: Optional[float] = Field(default=None, description="耗时毫秒，可缺省")
    truncated: Optional[bool] = Field(default=None, description="结果是否截断，可缺省")


class ConsolidationHistoryItem(ContractModel):
    """记忆合并结果（ExtractedMemory 契约视图，type 为 MemoryType 枚举值转 str）。"""

    type: str = Field(description="记忆类型（fact/plan/preference/event/note）")
    content: Optional[str] = Field(default=None, description="记忆内容，可缺省")
    poignancy: Optional[int] = Field(default=None, description="重要性 1-10，可缺省")


# ─────────────────────── 分组子对象（面板 props） ───────────────────────


class ConsolidationInfo(ContractModel):
    """Consolidator 面板 props（面板 13）。"""

    buffer_size: int = Field(description="缓冲消息对数")
    threshold: int = Field(description="触发合并阈值")
    history: list[ConsolidationHistoryItem] = Field(default_factory=list, description="最近合并结果")


class MemoryReflectorInfo(ContractModel):
    """记忆反射器面板 props（面板 14）。"""

    exists: bool = Field(description="反射器是否存在")
    round_count: int = Field(description="已运行轮次")
    round_threshold: int = Field(description="轮次触发阈值")
    new_memory_count: int = Field(description="新记忆计数")
    memory_threshold: int = Field(description="新记忆触发阈值")
    locked: bool = Field(description="是否锁定")
    last_summary: Optional[dict[str, Any]] = Field(default=None, description="最近摘要，可缺省")


class CharacterReflectorInfo(ContractModel):
    """性格反射器面板 props（面板 15）。"""

    exists: bool = Field(description="反射器是否存在")
    buffer_size: int = Field(description="缓冲对话对数")
    last_reflection_at: Optional[float] = Field(
        default=None, description="上次反射 Unix 时间戳，可缺省")
    locked: bool = Field(description="是否锁定")
    self_perception_history: list[Any] = Field(default_factory=list, description="自我认知历史")
    last_prompt_system: str = Field(default="", description="上次系统提示词")
    last_prompt_user: str = Field(default="", description="上次用户提示词")
    last_text_len: int = Field(default=0, description="上次文本长度")
    last_duration_ms: float = Field(default=0.0, description="上次耗时毫秒")
    last_success: bool = Field(default=False, description="上次是否成功")


class CharacterEngineInfo(ContractModel):
    """4 层性格引擎面板 props（面板 16）。"""

    exists: bool = Field(description="引擎是否存在")
    self_l2: str = Field(description="L2 Self 演化内容")
    state_l3: str = Field(description="L3 State 短期状态")
    mood: str = Field(description="L4 心情标签")
    last_awareness_time: Optional[float] = Field(
        default=None, description="C9 上次触发 Unix 时间戳，可缺省")
    l2_version_count: int = Field(default=0, description="L2 版本历史总数")
    c6_version_count: int = Field(default=0, description="C6 版本历史总数")
    state_history: list[Any] = Field(default_factory=list, description="L3 历史")


class PromptSummary(ContractModel):
    """prompt 摘要容器（records + 计数，recorder 未初始化时为空形状）。"""

    records: list[PromptRecord] = Field(default_factory=list, description="摘要记录列表")
    total: int = Field(default=0, description="记录总数")
    max: int = Field(default=0, description="环形缓冲容量")


class ResponseSummary(ContractModel):
    """响应摘要容器（records + 计数）。"""

    records: list[ResponseRecord] = Field(default_factory=list, description="摘要记录列表")
    total: int = Field(default=0, description="记录总数")
    max: int = Field(default=0, description="环形缓冲容量")


# ─────────────────────── DebugData（概览聚合） ───────────────────────


class DebugData(ContractModel):
    """Debug 概览聚合数据（16 面板平铺字段，对照 DebugInfo dataclass）。"""

    # System（面板 1）
    fsm_state: str = Field(default="idle", description="FSM 状态名")
    fsm_previous: Optional[str] = Field(default=None, description="上一 FSM 状态名，可缺省")
    uptime_seconds: float = Field(default=0.0, description="服务运行时长秒")
    processing: bool = Field(default=False, description="是否有任务在处理中")
    llm_model: str = Field(default="", description="当前 LLM 模型")
    tts_ok: bool = Field(default=False, description="TTS 后端可用")
    stt_ok: bool = Field(default=False, description="STT 后端可用")
    ws_clients: int = Field(default=0, description="WebSocket 客户端数")
    # IdleMonitor
    proactive_enabled: bool = Field(default=False, description="主动搭话开关")
    proactive_interval: Optional[float] = Field(default=None, description="主动搭话间隔秒，可缺省")
    last_interaction_ago: Optional[float] = Field(default=None, description="距上次交互秒，可缺省")
    # Session（面板 2）
    session_messages: int = Field(default=0, description="会话消息数")
    session_recent: list[SessionRecentItem] = Field(default_factory=list, description="最近消息")
    character_state_raw: dict[str, Any] = Field(default_factory=dict, description="角色状态原始 JSON")
    character_name: str = Field(default="", description="角色名")
    base_personality_summary: str = Field(default="", description="L1 基础人格摘要")
    archive_msg_count: int = Field(default=0, description="归档消息总数")
    # Long-term memory
    lt_count: int = Field(default=0, description="长期记忆总数")
    lt_recent: list[LtmRecentItem] = Field(default_factory=list, description="最近长期记忆")
    milvus_ok: bool = Field(default=False, description="Milvus 可用")
    embedding_ok: bool = Field(default=False, description="Embedding 可用")
    mongodb_ok: bool = Field(default=False, description="MongoDB 可用")
    # Tools（面板 11/12）
    currently_executing: list[str] = Field(default_factory=list, description="正在执行的工具")
    tools: list[ToolItem] = Field(default_factory=list, description="工具插件列表")
    tool_history: list[ToolHistoryItem] = Field(default_factory=list, description="工具执行历史")
    # Consolidation（面板 13）
    consolidator_buffer_size: int = Field(default=0, description="Consolidator 缓冲对数")
    consolidator_threshold: int = Field(default=0, description="Consolidator 触发阈值")
    consolidation_history: list[ConsolidationHistoryItem] = Field(
        default_factory=list, description="最近合并结果")
    # MemoryReflector（面板 14）
    mreflector_exists: bool = Field(default=False, description="记忆反射器是否存在")
    mreflector_round_count: int = Field(default=0, description="已运行轮次")
    mreflector_round_threshold: int = Field(default=0, description="轮次触发阈值")
    mreflector_new_memory_count: int = Field(default=0, description="新记忆计数")
    mreflector_memory_threshold: int = Field(default=0, description="新记忆触发阈值")
    mreflector_locked: bool = Field(default=False, description="是否锁定")
    mreflector_last_summary: Optional[dict[str, Any]] = Field(
        default=None, description="最近摘要，可缺省")
    # Character Reflector（面板 15）
    reflector_exists: bool = Field(default=False, description="性格反射器是否存在")
    reflector_buffer_size: int = Field(default=0, description="缓冲对话对数")
    reflector_last_reflection_at: Optional[float] = Field(
        default=None, description="上次反射 Unix 时间戳，可缺省")
    reflector_locked: bool = Field(default=False, description="是否锁定")
    self_perception_history: list[Any] = Field(default_factory=list, description="自我认知历史")
    reflector_last_prompt_system: str = Field(default="", description="上次系统提示词")
    reflector_last_prompt_user: str = Field(default="", description="上次用户提示词")
    reflector_last_success: bool = Field(default=False, description="上次是否成功")
    reflector_last_duration_ms: float = Field(default=0.0, description="上次耗时毫秒")
    reflector_last_text_len: int = Field(default=0, description="上次文本长度")
    # CharacterEngine（面板 16）
    character_engine_exists: bool = Field(default=False, description="4 层引擎是否存在")
    self_l2: str = Field(default="", description="L2 Self 演化内容")
    state_l3: str = Field(default="", description="L3 State 短期状态")
    state_history: list[Any] = Field(default_factory=list, description="L3 历史")
    last_awareness_time: Optional[float] = Field(
        default=None, description="C9 上次触发 Unix 时间戳，可缺省")
    l2_version_count: int = Field(default=0, description="L2 版本历史总数")
    c6_version_count: int = Field(default=0, description="C6 版本历史总数")
    # Token（面板 6）
    token_stats: DebugTokenStats = Field(default_factory=DebugTokenStats, description="会话 token 统计")
    pending_token_count: int = Field(default=0, description="待处理 token 快照数")
    pending_session_exists: bool = Field(default=False, description="是否存在待处理 session")
    # Prompt/Response 记录（面板 7/8）
    prompt_summary: PromptSummary = Field(default_factory=PromptSummary, description="prompt 摘要")
    response_summary: ResponseSummary = Field(default_factory=ResponseSummary, description="响应摘要")
    # Latency（面板 3）
    timing_history: list[Any] = Field(default_factory=list, description="延迟环形历史")
    timing_stats: TimingStats = Field(default_factory=TimingStats, description="延迟聚合统计")
    # 进程内存（面板 4）
    memory: ProcessMemory = Field(default_factory=ProcessMemory, description="进程内存占用")
    # Mood（L4 Emotion）
    mood: str = Field(default="", description="当前心情标签")
    # Scheduler（面板 5）
    scheduler_pending: int = Field(default=0, description="待执行任务数")
    scheduler_running: int = Field(default=0, description="运行中任务数")
    scheduler_total: int = Field(default=0, description="累计任务数")
    task_tree: dict[str, Any] = Field(default_factory=dict, description="任务 DAG 树")


# ─────────────────────── 数据库状态（/debug/api/db-status） ───────────────────────


class DbStatusLtm(ContractModel):
    """长期记忆库状态。"""

    count: int = Field(description="记忆条数")
    milvus_ok: bool = Field(description="Milvus 健康检查")
    dirty_count: int = Field(description="待落盘脏 ID 数")


class DbStatusSession(ContractModel):
    """会话库状态。"""

    count: int = Field(description="会话数")
    current_id: str = Field(description="当前会话 ID")
    mongodb_ok: bool = Field(description="MongoDB 健康检查")


class DbStatusArchive(ContractModel):
    """归档库状态。"""

    count: int = Field(description="归档消息数")
    mongodb_ok: bool = Field(description="MongoDB 健康检查")


class DbStatus(ContractModel):
    """/debug/api/db-status 响应：ltm/session/archive 三库状态。"""

    ltm: DbStatusLtm = Field(description="长期记忆库状态")
    session: DbStatusSession = Field(description="会话库状态")
    archive: DbStatusArchive = Field(description="归档库状态")


# ─────────────────────── LTM CRUD（/debug/api/ltm） ───────────────────────


class LtmEntry(ContractModel):
    """长期记忆单条（ltm 列表，含 timestamp；lt_recent 面板条目不含）。"""

    id: str = Field(description="记忆 ID")
    text: str = Field(description="记忆文本")
    memory_type: str = Field(description="记忆类型（fact/plan/preference/event/note）")
    poignancy: float = Field(description="重要性 1-10")
    timestamp: float = Field(default=0.0, description="Unix 时间戳")
    metadata: Optional[dict[str, Any]] = Field(default=None, description="附加元数据，可缺省")


class LtmListResponse(ContractModel):
    """/debug/api/ltm 列表响应。"""

    total: int = Field(description="命中总数")
    entries: list[LtmEntry] = Field(default_factory=list, description="当前页条目")


# ─────────────────────── 通用增删改结果 ───────────────────────


class DebugResult(ContractModel):
    """Debug 增删改操作通用结果（ok 判别 + 可选计数）。"""

    ok: bool = Field(description="操作是否成功")
    deleted: Optional[int] = Field(default=None, description="删除/清空计数，可缺省")
    session_purged: Optional[int] = Field(default=None, description="同步清理的 session 消息数，可缺省")


class RollbackResult(ContractModel):
    """角色版本回滚结果。"""

    status: str = Field(description="状态（恒为 ok）")
    new_version: int = Field(description="回滚产生的新版本号")
    text: str = Field(description="恢复的版本文本")


# ─────────────────────── 归档 / 会话（/debug/api/archive、/sessions） ───────────────────────


class ArchiveListResponse(ContractModel):
    """归档消息分页响应（messages 复用 rest.Message 契约视图）。"""

    messages: list[ArchiveMessage] = Field(default_factory=list, description="归档消息列表")
    total: int = Field(description="总消息数")
    limit: int = Field(description="每页条数")
    skip: int = Field(description="偏移量")


class SessionInfo(ContractModel):
    """会话列表条目。"""

    session_id: str = Field(description="会话 ID")
    message_count: int = Field(description="消息条数")
    updated_at: str = Field(default="", description="最近更新时间 ISO 字符串")
    token_total: int = Field(default=0, description="token 总用量")


class SessionListResponse(ContractModel):
    """会话列表响应。"""

    sessions: list[SessionInfo] = Field(default_factory=list, description="会话条目列表")


class SessionDetailMessage(ContractModel):
    """会话详情单条消息（契约视图；兼容 sessions_to_client 与 messages_for_client 两来源）。"""

    role: str = Field(description="消息角色（user/assistant/tool）")
    timestamp: Optional[str] = Field(default=None, description="ISO 时间戳，可缺省")
    content_type: Optional[dict[str, Any]] = Field(
        default=None, description="内容类型 → 序号映射，可缺省")
    parts: list[DebugSessionPart] = Field(default_factory=list, description="纯文本 parts 列表")
    content: Any = Field(default="", description="消息内容（文本或内容块列表）")


class SessionDetailResponse(ContractModel):
    """会话详情响应。"""

    session_id: str = Field(description="会话 ID")
    messages: list[SessionDetailMessage] = Field(default_factory=list, description="消息列表")
    params: Optional[dict[str, Any]] = Field(default=None, description="角色状态参数，可缺省")
    total: Optional[int] = Field(default=None, description="消息总数（归档源时存在），可缺省")


# ─────────────────────── 录制（/debug/api/prompts、/responses） ───────────────────────


class PromptDetail(ContractModel):
    """完整 prompt 记录详情。"""

    index: int = Field(description="记录序号")
    timestamp: str = Field(description="ISO 时间戳")
    round_num: int = Field(description="轮次序号")
    model: str = Field(description="模型名")
    messages: list[Any] = Field(default_factory=list, description="LLM 消息列表")
    tools: list[Any] = Field(default_factory=list, description="工具定义列表")
    template_source: Optional[dict[str, Any]] = Field(
        default=None, description="模板源码（path + sections），可缺省")


class ResponseDetail(ContractModel):
    """完整原始回复详情。"""

    index: int = Field(description="记录序号")
    timestamp: str = Field(description="ISO 时间戳")
    round_num: int = Field(description="轮次序号")
    raw_text: str = Field(description="原始回复文本")
    inner_voice: Optional[str] = Field(default=None, description="内心独白，可缺省")
    text_len: int = Field(default=0, description="回复文本长度")
    inner_voice_len: int = Field(default=0, description="内心独白长度")


# ─────────────────────── 角色版本（/debug/api/character-versions） ───────────────────────


class CharacterVersionListResponse(ContractModel):
    """角色版本历史列表响应（versions 保留 VersionEntry 全部 wire 键）。"""

    versions: list[Any] = Field(default_factory=list, description="版本条目列表")
    total: int = Field(default=0, description="版本总数")
    has_more: bool = Field(default=False, description="是否还有更早版本")


# ─────────────────────── 任务快照（/debug/api/tasks，WorkflowDebugAPI） ───────────────────────


class TaskNode(ContractModel):
    """任务 DAG 节点。"""

    node_id: str = Field(description="节点 ID")
    status: str = Field(description="节点状态")
    attempt: int = Field(default=0, description="尝试次数")
    summary: str = Field(default="", description="节点摘要")
    error: Optional[str] = Field(default=None, description="节点错误，可缺省")


class TaskData(ContractModel):
    """任务快照条目（TasksTab；workflow_engine 快照 wire 形状 + 前端 title 展示字段）。"""

    task_id: str = Field(description="任务 ID")
    status: str = Field(description="任务状态")
    title: Optional[str] = Field(default=None, description="任务标题（前端展示用），可缺省")
    plan_version: int = Field(default=0, description="计划版本号")
    created_at: Optional[str] = Field(default=None, description="创建时间，可缺省")
    error: Optional[str] = Field(default=None, description="任务错误，可缺省")
    error_code: Optional[str] = Field(default=None, description="任务错误码，可缺省")
    phase: str = Field(default="", description="当前执行阶段")
    mailbox_depth: int = Field(default=0, description="邮箱待处理消息数")
    nodes: list[TaskNode] = Field(default_factory=list, description="DAG 节点列表")
    edges: list[list[str]] = Field(default_factory=list, description="DAG 边列表 [from, to]")
