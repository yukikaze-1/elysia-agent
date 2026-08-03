// ---- 契约类型（单源：frontend_contract → contract.generated.ts，勿手改） ----
// ServerMessage 及全部成员、支持类型、客户端消息均从生成文件 re-export。
export * from './contract.generated'
// ContentPart 别名需要引用生成文件内的 TextPart/ImagePart（export * 不绑定本地名）
import type { TextPart, ImagePart } from './contract.generated'

// ---- 纯前端辅助类型（非契约生成，保留） ----
// CustomProvider / UnconfiguredEntry / ProvidersResponse / ImageUploadResponse 等
// REST 契约类型已由 frontend_contract/rest.py 建模 → contract.generated.ts（上方 export *），
// 不再手写声明，避免与生成文件重复声明（vue-tsc Duplicate identifier）。

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
