import { defineStore } from 'pinia'
import { ref } from 'vue'

/**
 * Minimal stub for audioStore.
 *
 * This is a placeholder created so that chatStore can import and reference
 * `useAudioStore()` inside function bodies (avoiding circular dependency).
 * Task 5 will replace this file with the full audioStore implementation
 * (TTS buffering, playback, mic, wake indicator).
 */
export const useAudioStore = defineStore('audio', () => {
  const audioPlaying = ref(false)
  const streamBubbles = ref<Record<number, string>>({})
  const audioQueue = ref<{ url: string; streamId?: number }[]>([])
  const audioChunks = ref<Record<number, Uint8Array[]>>({})
  const isMicActive = ref(false)

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
    setMicActive, flashWakeIndicator,
  }
})
