import { defineStore } from 'pinia'
import { ref } from 'vue'

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
    // 尊重 TTS 开关设置
    const settingsStore = useSettingsStore()
    if (!settingsStore.ttsEnabled) return

    const chunks = audioChunks.value[streamId]
    if (!chunks) return

    delete audioChunks.value[streamId]

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

    const chatStore = useChatStore()
    chatStore.revealNextQueued()

    const settingsStore = useSettingsStore()
    a.volume = settingsStore.volume / 100

    a.onended = () => {
      URL.revokeObjectURL(item.url)
      if (item.streamId != null) {
        delete streamBubbles.value[item.streamId]
      }
      audioPlaying.value = false
      chatStore.clearPlayingHighlight()
      playNext()
    }

    a.play().catch(() => {
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

  return {
    audioQueue, audioPlaying, audioChunks, streamBubbles, isMicActive,
    decodeBase64, bufferChunk, finalizeStream, playNext,
    setMicActive, flashWakeIndicator,
  }
})

// Lazy imports to avoid circular dependencies — placed here to signal that
// these are only ever called inside function bodies (never at module scope).
import { useChatStore } from './chatStore'
import { useSettingsStore } from './settingsStore'
