// ---- 契约类型（单源：frontend_contract → contract.generated.ts，勿手改） ----
// ServerMessage 及全部成员、支持类型、客户端消息均从生成文件 re-export。
export * from './contract.generated'
// ContentPart 别名需要引用生成文件内的 TextPart/ImagePart（export * 不绑定本地名）
import type { TextPart, ImagePart } from './contract.generated'

// ---- 纯前端辅助类型（非 WS 契约，保留） ----

/** 内容块联合别名——生成文件内联 `TextPart | ImagePart`，此处补别名供前端消费 */
export type ContentPart = TextPart | ImagePart

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
