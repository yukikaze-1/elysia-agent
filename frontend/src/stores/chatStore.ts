import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { ContentPart, ToolCallData, ToolCallResult, TokenUsage } from '@/types/protocol'
import { useAudioStore } from './audioStore'

let nextId = 1
function generateId(): string {
  return `msg_${Date.now()}_${nextId++}`
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string | ContentPart[]
  contentType?: 'text' | 'markdown' | 'code'
  language?: string
  timestamp: number
  toolCalls?: ToolCallData[]
  toolCallResults?: Record<string, ToolCallResult>
  innerVoice?: string
  tokenUsage?: TokenUsage
  pending?: boolean
  queued?: boolean
}

export const useChatStore = defineStore('chat', () => {
  const messages = ref<ChatMessage[]>([])
  const lastRole = ref('')
  const lastMsgTime = ref(0)
  const userScrolledUp = ref(false)
  const hasMoreMessages = ref(true)
  const earliestMessageId = ref<string | null>(null)
  const typing = ref(false)
  const mood = ref('')
  const moodIntensity = ref(0)
  const fsmState = ref('')
  const nextBubbleQueue = ref<string[]>([])
  const playingMessageId = ref<string | null>(null)
  const previewImages = ref<{ url: string; mime: string }[]>([])
  const sessionTokens = ref<{ total: number; byModel: Record<string, number> }>({ total: 0, byModel: {} })
  function appendDelta(text: string, streamId?: number) {
    const audioStore = useAudioStore()
    if (messages.value.length === 0 || lastRole.value !== 'assistant') {
      const msg: ChatMessage = {
        id: generateId(),
        role: 'assistant',
        content: text,
        timestamp: Date.now(),
        pending: true,
      }
      if (audioStore.audioPlaying) {
        msg.queued = true
        nextBubbleQueue.value.push(msg.id)
      }
      messages.value.push(msg)
      if (streamId != null) {
        audioStore.streamBubbles[streamId] = msg.id
      }
    } else {
      const last = messages.value[messages.value.length - 1]
      last.content += text
    }
    lastRole.value = 'assistant'
  }

  function addUserMessage(text: string) {
    finalizeDelta()
    nextBubbleQueue.value = []
    clearPlayingHighlight()
    const msg: ChatMessage = {
      id: generateId(),
      role: 'user',
      content: text,
      timestamp: Date.now(),
    }
    messages.value.push(msg)
    lastRole.value = 'user'
  }

  function addStatusMessage(text: string) {
    const msg: ChatMessage = {
      id: generateId(),
      role: 'system' as const,
      content: text,
      timestamp: Date.now(),
    }
    messages.value.push(msg)
  }

  function addToolCard(name: string, callId: string, args?: Record<string, unknown>) {
    const msg: ChatMessage = {
      id: generateId(),
      role: 'tool' as const,
      content: args ? JSON.stringify(args, null, 2) : '{}',
      timestamp: Date.now(),
      toolCalls: [{
        id: callId,
        function: { name, arguments: JSON.stringify(args || {}) },
      }],
    }
    messages.value.push(msg)
  }

  function updateToolCard(callId: string, data: ToolCallResult) {
    for (const msg of messages.value) {
      if (msg.role === 'tool' && msg.toolCalls?.[0]?.id === callId) {
        if (!msg.toolCallResults) msg.toolCallResults = {}
        msg.toolCallResults[callId] = {
          success: data.success !== false,
          content: data.content,
          error: data.error,
          error_type: data.error_type,
          duration_ms: data.duration_ms,
          truncated: data.truncated,
        }
        break
      }
    }
  }

  function handleCompleteMessage(msg: { content?: string; mood?: string }) {
    typing.value = false
    finalizeDelta()
    if (msg.mood) {
      messages.value.push({
        id: generateId(),
        role: 'system' as const,
        content: `心情: ${msg.mood}`,
        timestamp: Date.now(),
      })
    }
  }

  function addInnerVoice(content: string) {
    if (!content) return
    // 尊重内心独白开关设置
    const settingsStore = useSettingsStore()
    if (!settingsStore.innerVoiceEnabled) return
    messages.value.push({
      id: generateId(),
      role: 'system' as const,
      content,
      innerVoice: content,
      timestamp: Date.now(),
    })
  }

  function addBlockBubble(params: Record<string, unknown>) {
    const blockType = params.block_type as string || ''
    const content = params.content as string || ''
    const language = params.language as string || ''
    if (!content) return

    if (blockType === 'code') {
      messages.value.push({
        id: generateId(),
        role: 'assistant',
        content,
        contentType: 'code',
        language: language || '',
        timestamp: Date.now(),
      })
    } else {
      messages.value.push({
        id: generateId(),
        role: 'assistant',
        content,
        contentType: blockType === 'markdown' ? 'markdown' : undefined,
        timestamp: Date.now(),
      })
    }
  }

  function setMood(expr: string, intensity?: number) {
    mood.value = expr
    if (intensity != null) moodIntensity.value = intensity
  }

  function setState(stateName: string) {
    fsmState.value = stateName
    if (stateName === 'idle') {
      attachTokenLabel()
    }
  }

  function updateTokenUsage(msg: { turn?: { rounds: unknown[]; turn_total: number }; session?: unknown }) {
    if (msg.turn) {
      const last = messages.value[messages.value.length - 1]
      if (last && last.role === 'assistant') {
        last.tokenUsage = {
          rounds: msg.turn.rounds || [],
          turn_total: msg.turn.turn_total,
        } as unknown as TokenUsage
      }
    }
    // 更新 session 累计 token 统计
    if (msg.session) {
      sessionTokens.value = msg.session as { total: number; byModel: Record<string, number> }
    }
  }

  function attachTokenLabel() {
    // 在最新的 assistant 消息上已有 tokenUsage，UI 自动渲染
    // 此函数保留用于扩展
  }

  function finalizeDelta() {
    lastRole.value = ''
  }

  function clearPlayingHighlight() {
    playingMessageId.value = null
  }

  function revealNextQueued() {
    if (!nextBubbleQueue.value.length) {
      playingMessageId.value = null
      return
    }
    const id = nextBubbleQueue.value.shift()!
    const msg = messages.value.find(m => m.id === id)
    if (msg) msg.queued = false
    playingMessageId.value = id
  }

  function restoreFromServer(serverMessages: any[], _tokenStats?: any) {
    messages.value = []
    lastRole.value = ''
    lastMsgTime.value = 0
    nextBubbleQueue.value = []
    playingMessageId.value = null

    for (const m of serverMessages) {
      if (m.role === 'tool') {
        const toolMsg: ChatMessage = {
          id: generateId(),
          role: 'tool',
          content: m.content || '',
          timestamp: Date.parse(m.created_at) || Date.now(),
          toolCalls: [{
            id: m.tool_call_id,
            function: { name: m.name || 'tool', arguments: '{}' },
          }],
        }
        toolMsg.toolCallResults = {}
        toolMsg.toolCallResults[m.tool_call_id] = {
          success: m.success !== false,
          content: m.content || '',
          error: m.error,
          error_type: m.error_type,
          duration_ms: m.duration_ms || 0,
          truncated: m.truncated || false,
        }
        messages.value.push(toolMsg)
        continue
      }

      // 内心独白在公开回复之前单独渲染（尊重开关设置）
      const settingsStore = useSettingsStore()
      if (m.inner_voice && settingsStore.innerVoiceEnabled) {
        messages.value.push({
          id: generateId(),
          role: 'system',
          content: '',
          innerVoice: m.inner_voice,
          timestamp: Date.parse(m.created_at) || Date.now(),
        } as ChatMessage)
      }

      // 每个 part 独立一个气泡（一句一个，保持分句显示）
      const parts: any[] = m.parts || []
      const contentParts: any[] = (Array.isArray(m.content) ? m.content : [])
      // 收集 content 中的 image parts（如果有）
      const imageParts = contentParts.filter((p: any) => p && p.type === 'image' && p.src)

      for (let i = 0; i < parts.length; i++) {
        const p = parts[i]
        if (!p.content) continue
        const text = p.content
        // 舞台指示拆分：检测中文括号包裹的开头文本
        const split = splitAction(text)
        if (split) {
          messages.value.push({
            id: generateId(),
            role: m.role as ChatMessage['role'],
            content: split.action,
            timestamp: Date.parse(m.created_at) || Date.now(),
          } as ChatMessage)
          if (split.rest) {
            messages.value.push({
              id: generateId(),
              role: m.role as ChatMessage['role'],
              content: split.rest,
              timestamp: Date.parse(m.created_at) || Date.now(),
              tokenUsage: (i === parts.length - 1 && imageParts.length === 0) ? m.token_usage : undefined,
            } as ChatMessage)
          }
        } else {
          messages.value.push({
            id: generateId(),
            role: m.role as ChatMessage['role'],
            content: text,
            timestamp: Date.parse(m.created_at) || Date.now(),
            // 只在最后一句且没有图片时附加 token 统计
            tokenUsage: (i === parts.length - 1 && imageParts.length === 0) ? m.token_usage : undefined,
          } as ChatMessage)
        }
      }

      // 渲染图片 content parts（如果有）
      for (const img of imageParts) {
        messages.value.push({
          id: generateId(),
          role: m.role as ChatMessage['role'],
          content: [{ type: 'image', src: img.src, mime: img.mime }] as any,
          timestamp: Date.parse(m.created_at) || Date.now(),
          tokenUsage: m.token_usage,
        } as ChatMessage)
      }
    }

    finalizeDelta()
  }

  function loadMoreMessages() {
    // 后续迭代：fetch('/api/messages?limit=50&before=...')
  }

  const visibleMessages = computed(() => {
    return messages.value.filter(m =>
      !m.queued || m.id === playingMessageId.value
    )
  })

  const latestAssistantMessage = computed(() => {
    for (let i = messages.value.length - 1; i >= 0; i--) {
      if (messages.value[i].role === 'assistant') return messages.value[i]
    }
    return null
  })

  return {
    messages, lastRole, lastMsgTime, userScrolledUp, hasMoreMessages,
    earliestMessageId, typing, mood, moodIntensity, fsmState,
    nextBubbleQueue, playingMessageId, visibleMessages, latestAssistantMessage,
    previewImages, sessionTokens,
    appendDelta, addUserMessage, addStatusMessage, addToolCard, updateToolCard,
    handleCompleteMessage, addInnerVoice, addBlockBubble, setMood, setState,
    updateTokenUsage, attachTokenLabel, finalizeDelta, clearPlayingHighlight,
    revealNextQueued, restoreFromServer, loadMoreMessages,
  }
})

// ---- 工具函数 ----

/** 中文舞台指示检测正则：匹配括号包裹的中文动作描述 */
const CN_ACTION_RE = /[(（][^)）]*[一-鿿][^)）]*[)）]/

/**
 * 如果文本以中文舞台指示开头，返回 {action, rest}，否则 null。
 * 例如 "(轻声地)你好" → {action: "(轻声地)", rest: "你好"}
 */
function splitAction(text: string): { action: string; rest: string } | null {
  const m = text.match(CN_ACTION_RE)
  if (m && text.startsWith(m[0])) {
    const action = m[0]
    const rest = text.slice(action.length).trim()
    return { action, rest: rest || '' }
  }
  return null
}

// Lazy imports to avoid circular dependencies — placed here to signal that
// these are only ever called inside function bodies (never at module scope).
import { useSettingsStore } from './settingsStore'
