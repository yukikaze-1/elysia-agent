// ---- Supporting Types ----

export interface SessionMessageItem {
  role: string
  parts?: { content: string }[]
  content?: string | ContentPart[]
  content_type?: Record<string, number[]>
  mood?: string
  tool_calls?: ToolCallData[]
  inner_voice?: string
  // tool role fields
  tool_call_id?: string
  name?: string
  success?: boolean
  error?: string | null
  error_type?: string | null
  duration_ms?: number
  truncated?: boolean
  // token usage attached to messages
  token_usage?: TokenUsage
}

export interface TokenRound {
  model?: string
  input_tokens: number
  output_tokens: number
  total_tokens: number
  cached_tokens?: number
  reasoning_tokens?: number
  round_num: number
  timestamp?: string
}

export interface TokenStats {
  total: number
  by_model: Record<string, number>
}

export type ContentPart =
  | { type: 'text'; content: string }
  | { type: 'image'; src: string; mime?: string }

export interface ToolCallData {
  id: string
  function: {
    name: string
    arguments: string // JSON string, not parsed
  }
}

export interface ToolCallResult {
  success?: boolean // defaults to true when not provided
  content?: string
  error?: string | null
  error_type?: string | null
  duration_ms?: number
  truncated?: boolean
}

export interface TokenUsage {
  rounds: TokenRound[]
  turn_total: number
}

export interface PluginInfo {
  name: string
  description: string
  safety?: {
    requires_human_approval?: boolean
    max_concurrency?: number
    network_allowed?: boolean
    filesystem_allowed?: boolean
  }
  capabilities?: string[]
}

export interface CustomProvider {
  name: string
  base_url: string
  api_key_env?: string
  models?: string[]
  service?: string
}

export interface UnconfiguredEntry {
  name: string
  env_vars: string[]
}

export interface ProvidersResponse {
  llm: Record<string, { models: string[] }>
  stt: Record<string, { models: string[] }>
  tts: Record<string, { models: string[] }>
  custom?: {
    llm?: CustomProvider[]
    stt?: CustomProvider[]
    tts?: CustomProvider[]
  }
  unconfigured?: {
    llm?: UnconfiguredEntry[]
    stt?: UnconfiguredEntry[]
    tts?: UnconfiguredEntry[]
  }
}

// ---- Server Messages ----

export interface SessionMessage {
  type: 'session'
  messages: SessionMessageItem[]
  token_stats?: TokenStats
}

/** 单服务的运行时能力（available=false 时 model 为 null，服务端权威） */
export interface ServiceCapability {
  available: boolean
  enabled: boolean
  model: string | null
  default: string
}

export interface CapabilitiesMessage {
  type: 'capabilities'
  live2d_static_image: string
  services: { llm: ServiceCapability; stt: ServiceCapability; tts: ServiceCapability }
}

/** 客户端 → 服务端：设置服务开关（stt/tts 后端模式） */
export interface SetServiceMessage {
  type: 'set_service'
  service: 'stt' | 'tts'
  enabled: boolean
}

/** 服务端 → 客户端：服务开关操作结果（ok=false 时前端回滚乐观切换） */
export interface ServiceModeResultMessage {
  type: 'service_mode_result'
  service: 'stt' | 'tts' | 'llm'
  enabled: boolean
  ok: boolean
  error: string | null
}

export interface DeltaMessage {
  type: 'delta'
  content: string
  stream_id?: number
}

export interface MessageMessage {
  type: 'message'
  role?: string
  content?: string
  mood?: string
}

export interface TokenUsageMessage {
  type: 'token_usage'
  turn?: {
    rounds?: TokenRound[]
    turn_total?: number
  }
  session?: TokenStats
}

export interface ExpressionMessage {
  type: 'expression'
  expression: string
  intensity?: number
}

export interface StateMessage {
  type: 'state'
  state: string
}

export interface AudioMessage {
  type: 'audio'
  stream_id: number
  seq: number
  data: string
}

export interface AudioEndMessage {
  type: 'audio_end'
  stream_id: number
  format?: 'wav' | 'mp3'
}

export interface TypingMessage {
  type: 'typing'
  typing: boolean
}

export interface TranscriptionMessage {
  type: 'transcription'
  text: string
  emotion?: string
  events?: string[]
}

export interface ActionMessage {
  type: 'action'
  action: 'tool_executing' | 'tool_done' | string
  params: Record<string, unknown>
}

export interface CommandMessage {
  type: 'command'
  command: 'tool_approval' | 'block' | 'inner_voice'
  params?: Record<string, unknown>
}

export interface WakeMessage {
  type: 'wake'
}

export interface SwitchModelResultMessage {
  type: 'switch_model_result'
  service: string
  model: string
  ok: boolean
  error?: string | null
}

export interface HeartbeatAckMessage {
  type: 'heartbeat_ack'
}

export interface ErrorMessage {
  type: 'error'
  message: string
}

export interface UnknownMessage {
  type: string
  [key: string]: unknown
}

// ---- Server Message Union ----
// Discriminated by the `type` field

export type ServerMessage =
  | SessionMessage
  | CapabilitiesMessage
  | DeltaMessage
  | MessageMessage
  | TokenUsageMessage
  | ExpressionMessage
  | StateMessage
  | AudioMessage
  | AudioEndMessage
  | TypingMessage
  | TranscriptionMessage
  | ActionMessage
  | CommandMessage
  | WakeMessage
  | SwitchModelResultMessage
  | ServiceModeResultMessage
  | HeartbeatAckMessage
  | ErrorMessage
  | UnknownMessage
