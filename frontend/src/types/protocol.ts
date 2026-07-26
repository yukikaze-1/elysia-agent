// ---- Supporting Types ----

export interface SessionMessageItem {
  role: 'user' | 'assistant' | 'tool'
  parts?: { content: string }[]
  content?: string | ContentPart[]
  content_type?: Record<string, unknown>
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
  model: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  round_num: number
  timestamp: number
}

export interface TokenStats {
  total: number
  byModel?: Record<string, number>
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
  turn: {
    rounds: TokenRound[]
    turn_total: number
  }
  session: TokenStats
}

export interface PluginInfo {
  name: string
  description: string
  safety?: Record<string, unknown>
  capabilities?: string[]
}

export interface CustomProvider {
  name: string
  base_url: string
  api_key_env?: string
  models?: string[]
  service?: string
}

// ---- Server Messages ----

export interface SessionMessage {
  type: 'session'
  messages: SessionMessageItem[]
  token_stats?: TokenStats
}

export interface CapabilitiesMessage {
  type: 'capabilities'
  stt?: boolean
  tts?: boolean
  llm_model?: string
  live2d_static_image?: string
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
  intensity: number
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
  action: string
  params: Record<string, unknown>
}

export interface CommandMessage {
  type: 'command'
  command: string
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
  | HeartbeatAckMessage
  | ErrorMessage
  | UnknownMessage
