import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

export interface AudioQueueItem {
  url: string
  streamId?: number
}

export const useAudioStore = defineStore('audio', () => {
  const audioQueue = ref<AudioQueueItem[]>([])
  const audioPlaying = ref(false)
  const audioChunks = ref<Record<number, Uint8Array[]>>({})
  const streamBubbles = ref<Record<number, string>>({})
  const isMicActive = ref(false)
  // 当前播放中的 Audio 元素引用（取消播放用；未播放时为 null）
  const currentAudio = ref<HTMLAudioElement | null>(null)

  function decodeBase64(b64: string): Uint8Array {
    const binary = atob(b64)
    const bytes = new Uint8Array(binary.length)
    for (let i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i)
    }
    return bytes
  }

  function bufferChunk(streamId: number, _seq: number, data: string) {
    if (!audioChunks.value[streamId]) {
      audioChunks.value[streamId] = []
    }
    audioChunks.value[streamId].push(decodeBase64(data))
  }

  function finalizeStream(streamId: number, format: string = 'wav') {
    // Always clean up chunk storage, even when TTS is off
    const chunks = audioChunks.value[streamId]
    delete audioChunks.value[streamId]
    if (!chunks) return

    const settingsStore = useSettingsStore()
    if (!settingsStore.ttsEnabled) return

    // 设置 stream bubble 映射的 TTL 清理
    if (streamId != null) {
      setTimeout(() => {
        if (streamBubbles.value[streamId]) {
          delete streamBubbles.value[streamId]
        }
      }, 30000)
    }

    const totalLength = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
    const combined = new Uint8Array(totalLength)
    let offset = 0
    for (const chunk of chunks) {
      combined.set(chunk, offset)
      offset += chunk.length
    }

    const blob = new Blob([combined], { type: `audio/${format}` })
    const url = URL.createObjectURL(blob)
    audioQueue.value.push({ url, streamId })
    playNext()
  }

  function playNext() {
    if (audioPlaying.value || audioQueue.value.length === 0) return

    audioPlaying.value = true

    const item = audioQueue.value.shift()!
    const a = new Audio(item.url)
    currentAudio.value = a

    const chatStore = useChatStore()
    chatStore.revealNextQueued()

    const settingsStore = useSettingsStore()
    a.volume = settingsStore.volume / 100

    a.onended = () => {
      if (currentAudio.value === a) currentAudio.value = null
      URL.revokeObjectURL(item.url)
      if (item.streamId != null) {
        delete streamBubbles.value[item.streamId]
      }
      audioPlaying.value = false
      chatStore.clearPlayingHighlight()
      playNext()
    }

    a.play().catch(() => {
      if (currentAudio.value === a) currentAudio.value = null
      audioPlaying.value = false
      URL.revokeObjectURL(item.url)
      if (item.streamId != null) {
        delete streamBubbles.value[item.streamId]
      }
      playNext()
    })
  }

  function setMicActive(active: boolean) {
    isMicActive.value = active
  }

  function flashWakeIndicator() {
    const btn = document.getElementById('mic-btn')
    if (!btn) return
    btn.style.boxShadow = '0 0 12px 3px rgba(99, 200, 255, 0.9)'
    btn.style.transform = 'scale(1.15)'
    setTimeout(() => {
      btn.style.boxShadow = ''
      btn.style.transform = ''
    }, 800)
  }

  /**
   * 清理所有未完成的音频流数据。
   * 在 WS 断开或 TTS 关闭时调用，防止 audioChunks 中残留的 chunks 内存泄漏。
   */
  function cleanup() {
    // 取消当前播放的音频（若有），并释放其 Blob URL
    if (currentAudio.value) {
      currentAudio.value.pause()
      URL.revokeObjectURL(currentAudio.value.src)
      currentAudio.value = null
    }
    // 释放所有已累积的 chunk 内存
    for (const streamId of Object.keys(audioChunks.value)) {
      delete audioChunks.value[Number(streamId)]
    }
    // 释放所有已入队但未播放的 Blob URL
    for (const item of audioQueue.value) {
      URL.revokeObjectURL(item.url)
    }
    audioQueue.value = []
    audioPlaying.value = false
  }

  // TTS 模式被关闭 → 立即取消当前播放 + 清空队列（防御：后端停产后残余音频立刻静音）
  watch(
    () => useSettingsStore().ttsEnabled,
    (enabled) => {
      if (!enabled) cleanup()
    }
  )

  return {
    audioQueue, audioPlaying, audioChunks, streamBubbles, isMicActive,
    decodeBase64, bufferChunk, finalizeStream, playNext,
    setMicActive, flashWakeIndicator, cleanup,
  }
})

// ---- 循环依赖说明 ----
// audioStore ↔ chatStore 互相导入，形成循环依赖。
// 这是有意为之：两个 store 都需要在函数体内惰性调用对方的 getter（Pinia 惰性初始化允许这样做）。
// 约束：
//   1. useChatStore() / useAudioStore() 调用仅限于函数体内部，绝不能在模块顶层。
//   2. 如需提取共享逻辑，优先放入独立的 composable 或 utility 模块。
//   3. 新功能不得加剧此循环依赖；如需新增跨 store 调用，先评估是否可单向化。
// ---- Lazy imports（函数体内使用，避免模块顶层循环引用报错） ----
import { useChatStore } from './chatStore'
import { useSettingsStore } from './settingsStore'
