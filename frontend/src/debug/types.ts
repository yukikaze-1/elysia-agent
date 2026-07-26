// ---- 子结构 ----

export interface TokenRoundItem {
  model: string; prompt_tokens: number; completion_tokens: number
  total_tokens: number; round_num: number
}

export interface TokenStats {
  session_total: number; per_model: Record<string, number>
  current_turn: TokenRoundItem[]
}

export interface TimingStats {
  count: number; avg_ms: number; p50_ms: number; p95_ms: number; p99_ms: number; last_ms: number
}

/** 进程内存（rss/vms/pct/threads），不是"长期记忆" */
export interface ProcessMemory {
  rss_mb: number; vms_mb: number; pct: number; num_threads: number
}

export interface PromptRecord {
  index: number; timestamp: string; round_num: number
  message_count: number; system_prompt_len: number; total_chars: number; model: string
}

export interface ResponseRecord {
  index: number; timestamp: string; round_num: number; text_len: number; inner_voice_len: number
}

export interface ContentPart {
  type: string; content?: string; src?: string; mime?: string
}

export interface SessionRecentItem {
  role: string; parts: { content: string }[]; content: ContentPart[]
  content_type?: Record<string, unknown>
}

export interface LtmRecentItem {
  id: string; text: string; memory_type: string; poignancy: number
  metadata?: Record<string, unknown>
}

export interface ToolItem {
  name: string; description: string; parameters?: Record<string, unknown>
}

export interface ToolHistoryItem {
  name: string; args?: unknown; result?: unknown; error?: string
}

export interface ConsolidationHistoryItem {
  type: string; content?: string; [key: string]: unknown
}

// ---- 分组子对象（面板 props 类型标注） ----

export interface ConsolidationInfo {
  buffer_size: number; threshold: number; history: ConsolidationHistoryItem[]
}

export interface MemoryReflectorInfo {
  exists: boolean; round_count: number; round_threshold: number
  new_memory_count: number; memory_threshold: number
  locked: boolean; last_summary?: unknown
}

export interface CharacterReflectorInfo {
  exists: boolean; buffer_size: number; last_reflection_at?: number; locked: boolean
  self_perception_history?: unknown[]
  last_prompt_system?: string; last_prompt_user?: string
  last_text_len?: number; last_duration_ms?: number; last_success?: boolean
}

export interface CharacterEngineInfo {
  exists: boolean; self_l2: string; state_l3: string; mood: string
  last_awareness_time?: number; l2_version_count: number; c6_version_count: number
  state_history?: unknown[]
}

// ---- DebugData（与后端 DebugInfo dataclass 平铺字段一一对应） ----

export interface DebugData {
  fsm_state: string; fsm_previous: string | null; uptime_seconds: number; processing: boolean
  llm_model: string; tts_ok: boolean; stt_ok: boolean; ws_clients: number
  proactive_enabled: boolean; proactive_interval?: number; last_interaction_ago?: number
  session_messages: number; session_recent: SessionRecentItem[]
  character_state_raw: Record<string, unknown>
  character_name: string; base_personality_summary: string
  archive_msg_count: number; lt_count: number; lt_recent: LtmRecentItem[]
  milvus_ok: boolean; embedding_ok: boolean; mongodb_ok: boolean
  currently_executing: string[]; tools: ToolItem[]; tool_history: ToolHistoryItem[]
  consolidator_buffer_size: number; consolidator_threshold: number
  consolidation_history: ConsolidationHistoryItem[]
  mreflector_exists: boolean; mreflector_round_count: number; mreflector_round_threshold: number
  mreflector_new_memory_count: number; mreflector_memory_threshold: number
  mreflector_locked: boolean; mreflector_last_summary: unknown
  reflector_exists: boolean; reflector_buffer_size: number
  reflector_last_reflection_at: number | null; reflector_locked: boolean
  self_perception_history: unknown[]
  reflector_last_prompt_system: string; reflector_last_prompt_user: string
  reflector_last_success: boolean; reflector_last_duration_ms: number; reflector_last_text_len: number
  character_engine_exists: boolean; self_l2: string; state_l3: string
  state_history: unknown[]; last_awareness_time: number
  l2_version_count: number; c6_version_count: number
  token_stats: TokenStats; pending_token_count: number; pending_session_exists: boolean
  prompt_summary: { records: PromptRecord[]; total: number; max: number }
  response_summary: { records: ResponseRecord[]; total: number; max: number }
  timing_history: unknown[]; timing_stats: TimingStats
  /** 进程内存（不是"长期记忆"） */
  memory: ProcessMemory
  mood: string
  scheduler_pending: number; scheduler_running: number; scheduler_total: number; task_tree: unknown
}
