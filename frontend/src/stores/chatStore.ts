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
  messageId?: string
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
  // ---- KB-56：会话身份 + 世代 ----
  // `activeSessionId` = 当前 `messages` 数组所属的会话（来自 WS `session` 回放的
  // `conversationId`）。**它同时是"历史请求的归属冻结值"的权威来源**——不变量 SI1：
  // 异步的会话域响应只能改它**发起时**捕获的那个会话；SI2：切会话不会重定向在途
  // 请求；SI3：陈旧响应可以丢弃或存进原会话，但绝不能污染别的会话；SI4：修复不依赖
  // 响应到达顺序。
  //
  // `sessionEpoch` 是**世代计数器**（不是第二套会话权威，只是一张递增票）：每次会话
  // 归属改变 +1。请求发起时捕获该票，回来时票不同即判定为陈旧响应——即使新旧会话
  // 恰好同名（A→B→A）也不会被误认为同一次加载。只用 `activeSessionId !== captured`
  // 判据在 A→B→A 的瞬时情形下会漏（请求回来时会话又变回 A 了）。
  const activeSessionId = ref<string | null>(null)
  const sessionEpoch = ref(0)
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
    // 用户新消息打断播放：清空排队队列，并兜底清除所有 queued 标记
    // （防止音频中断/被移出队列时气泡永久隐藏）
    for (const m of messages.value) m.queued = false
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

  function updateTokenUsage(msg: { turn?: { rounds?: unknown[]; turn_total?: number }; session?: unknown }) {
    if (msg.turn) {
      const last = messages.value[messages.value.length - 1]
      if (last && last.role === 'assistant') {
        last.tokenUsage = {
          rounds: msg.turn.rounds || [],
          turn_total: msg.turn.turn_total,
        } as unknown as TokenUsage
      }
    }
    // 更新 session 累计 token 统计（后端字段为 byModel）
    if (msg.session) {
      const s = msg.session as { total: number; byModel: Record<string, number> }
      sessionTokens.value = { total: s.total, byModel: s.byModel }
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

  /** 移除单张预览图片 */
  function removePreviewImage(url: string) {
    previewImages.value = previewImages.value.filter(p => p.url !== url)
  }

  /** 清空全部预览图片 */
  function clearPreviewImages() {
    previewImages.value = []
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

  /**
   * 用服务端回放**替换**当前消息列表，并把列表归属冻结到该回放所属的会话。
   *
   * KB-56：`conversationId` 是回放帧自带的会话身份（契约 `SessionMessage.conversationId`，
   * 后端在 WS 建连/激活时发送）。此前调用方把它丢掉了，于是 store 没有任何会话身份，
   * `loadMoreMessages` 只能打**全库**端点 ⇒ 别会话的消息会进当前聊天。
   *
   * @param serverMessages 回放消息列表
   * @param tokenStats     会话级 token 统计（可缺省）
   * @param sessionId      本回放所属会话 ID；缺省/空串 ⇒ 归属未知（此时禁用翻页）
   */
  function restoreFromServer(
    serverMessages: any[],
    tokenStats?: { total: number; byModel: Record<string, number> },
    sessionId?: string | null,
  ) {
    messages.value = []
    lastRole.value = ''
    lastMsgTime.value = 0
    nextBubbleQueue.value = []
    playingMessageId.value = null
    // 归属改变 ⇒ 世代 +1（使一切在途的历史请求立即变成陈旧响应）
    activeSessionId.value = sessionId ? String(sessionId) : null
    sessionEpoch.value += 1
    // 回放项**不含 `_id`**（契约 SessionMessageItem 无该字段，pydantic 也会丢弃多余键），
    // 所以这里恒为 null ⇒ 第一次翻页不带 before，由会话域端点返回的 cursor 接手。
    earliestMessageId.value = serverMessages[serverMessages.length - 1]?._id || null

    // 恢复 session 累计 token（消除刷新/重连后 CharStatus 归零直到下次广播的缺口）
    if (tokenStats) {
      sessionTokens.value = { total: tokenStats.total, byModel: tokenStats.byModel }
    }

    for (const m of serverMessages) {
      const converted = convertServerMessage(m)
      for (const cm of converted) {
        messages.value.push(cm)
      }
    }

    finalizeDelta()
  }

  // 并发保护：防止多次快速触发导致的重复加载
  let _loadingMore = false

  /**
   * 加载更早的历史消息（滚动到顶部触发）。
   *
   * KB-56 的三条不变量：
   * - **SI1**：只打**会话域**端点 `/api/sessions/{id}/messages`；会话身份未知时直接
   *   拒绝加载（**绝不**退回全库 `/api/messages`——那是跨会话 compat 接口，返回全局
   *   最新 50 条，正是本缺陷的注入源）。
   * - **SI2/SI4**：请求发起时捕获 `(sessionId, epoch)`，响应回来先核对；票变了就是
   *   陈旧响应，**丢弃**（不写消息、不推进游标、不改 `hasMore`）。
   * - **SI3**：游标只在**本页确实推进了**时更新，且取会话域响应的 `cursor`
   *   （契约 `MessagesPageResponse.cursor`）。
   */
  async function loadMoreMessages(): Promise<boolean> {
    if (!hasMoreMessages.value || _loadingMore) return false
    const sid = activeSessionId.value
    if (!sid) {
      // 归属未知：宁可让"加载更多"暂时不可用，也不把别会话的消息灌进当前聊天。
      console.warn('加载历史消息已跳过：会话身份未知（未收到会话回放）')
      return false
    }
    const epochAtRequest = sessionEpoch.value
    _loadingMore = true
    const before = earliestMessageId.value
    try {
      const params = new URLSearchParams({ limit: '50' })
      if (before) params.set('before', before)
      const resp = await fetch(`/api/sessions/${encodeURIComponent(sid)}/messages?${params}`)
      if (!resp.ok) return false

      // ---- 陈旧响应闸门（SI2/SI3/SI4）：必须在**任何** store 变更之前判定 ----
      if (sessionEpoch.value !== epochAtRequest || activeSessionId.value !== sid) {
        console.warn('丢弃陈旧的历史消息响应（会话已切换）:', sid)
        return false
      }

      const data = await resp.json()
      const serverMsgs: any[] = data.messages || []

      // 已存在的 message_id 集合（session 窗口内消息可能同时存在于归档）
      const seen = new Set(messages.value.map(m => m.messageId).filter(Boolean))
      const olderMsgs: ChatMessage[] = []
      for (const m of serverMsgs) {
        const converted = convertServerMessage(m)
        for (const cm of converted) {
          if (cm.messageId && seen.has(cm.messageId)) continue  // 去重，避免与 session 窗口重叠
          if (cm.messageId) seen.add(cm.messageId)
          olderMsgs.push(cm)
        }
      }

      if (olderMsgs.length > 0) {
        // 保持旧消息在前
        messages.value = [...olderMsgs, ...messages.value]
        // 游标 = 本页最旧一条（会话域响应为 _id 降序；契约 cursor 指向 raw 边界）
        earliestMessageId.value = data.cursor || serverMsgs[serverMsgs.length - 1]?._id || null
      }
      hasMoreMessages.value = data.has_more !== false
      return olderMsgs.length > 0
    } catch (e) {
      console.warn('加载历史消息失败:', e)
      return false
    } finally {
      _loadingMore = false
    }
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
    previewImages, sessionTokens, activeSessionId, sessionEpoch,
    appendDelta, addUserMessage, addStatusMessage, addToolCard, updateToolCard,
    handleCompleteMessage, addInnerVoice, addBlockBubble, setMood, setState,
    updateTokenUsage, attachTokenLabel, finalizeDelta, clearPlayingHighlight,
    removePreviewImage, clearPreviewImages,
    revealNextQueued, restoreFromServer, loadMoreMessages,
  }
})

// ---- 工具函数 ----

/** 将服务端消息转为 ChatMessage 数组（一条服务端消息可拆成多条 UI 消息） */
function convertServerMessage(m: any): ChatMessage[] {
  const settingsStore = useSettingsStore()
  const out: ChatMessage[] = []

  if (m.role === 'tool') {
    const toolMsg: ChatMessage = {
      id: generateId(),
      role: 'tool',
      content: m.content || '',
      timestamp: Date.parse(m.timestamp) || Date.now(),
      messageId: m.message_id,
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
    out.push(toolMsg)
    return out
  }

  // 内心独白在公开回复之前单独渲染（尊重开关设置）
  if (m.inner_voice && settingsStore.innerVoiceEnabled) {
    out.push({
      id: generateId(),
      role: 'system',
      content: '',
      innerVoice: m.inner_voice,
      timestamp: Date.parse(m.timestamp) || Date.now(),
      messageId: m.message_id,
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
      out.push({
        id: generateId(),
        role: m.role as ChatMessage['role'],
        content: split.action,
        timestamp: Date.parse(m.timestamp) || Date.now(),
        messageId: m.message_id,
      } as ChatMessage)
      if (split.rest) {
        out.push({
          id: generateId(),
          role: m.role as ChatMessage['role'],
          content: split.rest,
          timestamp: Date.parse(m.timestamp) || Date.now(),
          messageId: m.message_id,
          tokenUsage: (i === parts.length - 1 && imageParts.length === 0) ? m.token_usage : undefined,
        } as ChatMessage)
      }
    } else {
      out.push({
        id: generateId(),
        role: m.role as ChatMessage['role'],
        content: text,
        timestamp: Date.parse(m.timestamp) || Date.now(),
        messageId: m.message_id,
        // 只在最后一句且没有图片时附加 token 统计
        tokenUsage: (i === parts.length - 1 && imageParts.length === 0) ? m.token_usage : undefined,
      } as ChatMessage)
    }
  }

  // 渲染图片 content parts（如果有）
  for (const img of imageParts) {
    out.push({
      id: generateId(),
      role: m.role as ChatMessage['role'],
      content: [{ type: 'image', src: img.src, mime: img.mime }] as any,
      timestamp: Date.parse(m.timestamp) || Date.now(),
      messageId: m.message_id,
      tokenUsage: m.token_usage,
    } as ChatMessage)
  }

  return out
}

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

// ---- 循环依赖说明 ----
// chatStore ↔ audioStore 互相导入，形成循环依赖。见 audioStore.ts 中对应注释的完整说明。
// 约束同上：所有跨 store 调用必须在函数体内进行，不得在模块顶层。
// ---- Lazy import（函数体内使用，避免模块顶层循环引用报错） ----
import { useSettingsStore } from './settingsStore'
