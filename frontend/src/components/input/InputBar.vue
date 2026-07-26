<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useConnectionStore } from '@/stores/connectionStore'
import { useChatStore } from '@/stores/chatStore'
import { useAudioStore } from '@/stores/audioStore'
import { useSettingsStore } from '@/stores/settingsStore'
import type { ContentPart } from '@/types/protocol'

const SEND_COOLDOWN_MS = 800
const INPUT_MAX_HEIGHT = 120
const MIC_BUFFER_SIZE = 4096
const MIC_SAMPLE_RATE = 16000

const textInput = ref('')
const cooldown = ref(false)
const fileInputRef = ref<HTMLInputElement | null>(null)
const textareaRef = ref<HTMLTextAreaElement | null>(null)

// Local mic state (not reactive — managed imperatively)
let micStream: MediaStream | null = null
let audioCtx: AudioContext | null = null
let scriptNode: ScriptProcessorNode | null = null

const connectionStore = useConnectionStore()
const chat = useChatStore()
const audioStore = useAudioStore()
const settingsStore = useSettingsStore()

// ---- Send ----

function sendText() {
  const text = textInput.value.trim()
  const hasImages = chat.previewImages.length > 0
  if (!text && !hasImages) return
  if (!connectionStore.connected) return

  if (hasImages) {
    chat.finalizeDelta()
    chat.nextBubbleQueue = []
    chat.clearPlayingHighlight()

    const content: ContentPart[] = chat.previewImages.map(img => ({
      type: 'image' as const,
      src: img.url,
      mime: img.mime,
    }))
    if (text) content.push({ type: 'text', content: text })

    connectionStore.send({ type: 'text', content })

    chat.$patch((state) => {
      state.messages.push({
        id: `msg_${Date.now()}_${Math.random()}`,
        role: 'user',
        content,
        timestamp: Date.now(),
      })
      state.lastRole = 'user'
      state.previewImages = []
    })
  } else {
    chat.addUserMessage(text)
    connectionStore.send({ type: 'text', content: text })
  }

  textInput.value = ''
  cooldown.value = true
  setTimeout(() => { cooldown.value = false }, SEND_COOLDOWN_MS)
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    if (!cooldown.value) sendText()
  } else if (e.ctrlKey && e.key === 'Escape') {
    e.preventDefault()
    connectionStore.send({ type: 'interrupt' })
  }
  requestAnimationFrame(autoResize)
}

function autoResize() {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, INPUT_MAX_HEIGHT) + 'px'
}

// ---- Mic ----

async function startMic() {
  if (!settingsStore.sttAvailable) {
    chat.addStatusMessage('STT 服务不可用，语音识别已禁用')
    return
  }
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: MIC_SAMPLE_RATE,
        channelCount: 1,
        echoCancellation: true,
      },
    })
    audioCtx = new (window.AudioContext || (window as any).webkitAudioContext)({ sampleRate: MIC_SAMPLE_RATE })
    const source = audioCtx.createMediaStreamSource(micStream)
    scriptNode = audioCtx.createScriptProcessor(MIC_BUFFER_SIZE, 1, 1)
    scriptNode.onaudioprocess = (e: AudioProcessingEvent) => {
      const input = e.inputBuffer.getChannelData(0)
      const int16 = new Int16Array(input.length)
      for (let i = 0; i < input.length; i++) {
        int16[i] = Math.max(-32768, Math.min(32767, input[i] * 32768))
      }
      connectionStore.sendBinary(int16.buffer)
    }
    source.connect(scriptNode)
    audioStore.setMicActive(true)
  } catch (err) {
    console.error('Mic start error:', err)
    let hint = '无法访问麦克风。'
    if ((err as DOMException).name === 'NotAllowedError') hint = '请允许浏览器使用麦克风权限。'
    else if ((err as DOMException).name === 'NotFoundError') hint = '未检测到麦克风设备。'
    chat.addStatusMessage(hint)
    stopMic()
  }
}

function stopMic() {
  if (scriptNode) { scriptNode.disconnect(); scriptNode = null }
  if (audioCtx) {
    audioCtx.close().catch(() => { /* AudioContext already closed */ })
    audioCtx = null
  }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null }
  audioStore.setMicActive(false)
}

function toggleMic() {
  if (micStream) { stopMic(); return }
  startMic()
}

// ---- Image upload ----

async function uploadImage(file: File) {
  const form = new FormData()
  form.append('image', file)
  try {
    const resp = await fetch('/api/images', { method: 'POST', body: form })
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}))
      chat.addStatusMessage('图片上传失败: ' + (err.error || resp.status))
      return
    }
    const { url, mime } = await resp.json()
    chat.previewImages.push({ url, mime })
  } catch (e) {
    chat.addStatusMessage('图片上传失败: ' + (e as Error).message)
  }
}

function onAttachClick() {
  fileInputRef.value?.click()
}

function onFileChange(e: Event) {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  if (file) uploadImage(file)
  target.value = ''
}

// ---- Paste / Drag-drop ----

function onPaste(e: ClipboardEvent) {
  const items = e.clipboardData?.items
  if (!items) return
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      e.preventDefault()
      const file = item.getAsFile()
      if (file) uploadImage(file)
      break
    }
  }
}

function onDragOver(e: DragEvent) {
  e.preventDefault()
}

function onDragEnter(e: DragEvent) {
  e.preventDefault()
}

function onDragLeave(e: DragEvent) {
  e.preventDefault()
}

function onDrop(e: DragEvent) {
  e.preventDefault()
  const file = e.dataTransfer?.files[0]
  if (file && file.type.startsWith('image/')) uploadImage(file)
}

// ---- Lifecycle ----

onMounted(() => {
  document.addEventListener('paste', onPaste)
  autoResize()
})

onUnmounted(() => {
  document.removeEventListener('paste', onPaste)
  if (micStream) stopMic()
})
</script>

<template>
  <button id="attach-btn" title="添加图片" @click="onAttachClick">📷</button>
  <input type="file" ref="fileInputRef" hidden accept="image/png,image/jpeg,image/gif,image/webp" @change="onFileChange" />
  <button
    id="mic-btn"
    title="麦克风"
    :class="{ active: audioStore.isMicActive }"
    @click="toggleMic"
  >🎤</button>
  <textarea
    ref="textareaRef"
    v-model="textInput"
    placeholder="输入消息..."
    rows="1"
    @keydown="handleKeydown"
    @input="autoResize"
    @dragover="onDragOver"
    @dragenter="onDragEnter"
    @dragleave="onDragLeave"
    @drop="onDrop"
  ></textarea>
  <button
    id="send-btn"
    :disabled="cooldown || !connectionStore.connected"
    :class="{ sent: cooldown }"
    @click="sendText"
  >➤</button>
</template>

<style scoped>
#attach-btn {
  width: 42px; height: 42px; border-radius: 50%;
  border: 1px solid var(--border); background: var(--surface);
  color: var(--muted); font-size: 18px; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: color 0.2s, border-color 0.2s;
  margin-right: 4px; flex-shrink: 0;
}
#attach-btn:hover { color: var(--text); border-color: var(--accent); }

textarea {
  flex: 1; padding: 10px 14px; border-radius: 20px; border: 1px solid var(--border);
  background: var(--bg); color: var(--text); font-size: 14px; outline: none; resize: none;
  height: 42px; max-height: 120px; line-height: 1.4; font-family: inherit;
}
textarea:focus { border-color: var(--accent); }

#mic-btn {
  width: 42px; height: 42px; border-radius: 50%; border: none; cursor: pointer; font-size: 18px;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: background .2s;
  background: var(--border); color: var(--text);
}
#mic-btn.active { background: var(--accent); animation: mic-pulse 1.5s infinite; }

@keyframes mic-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(233,69,96,.4); }
  50% { box-shadow: 0 0 0 8px rgba(233,69,96,0); }
}

#send-btn {
  width: 42px; height: 42px; border-radius: 50%; border: none; cursor: pointer; font-size: 18px;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: all 0.2s;
  background: var(--accent); color: white; position: relative;
}
#send-btn:hover { filter: brightness(1.1); }
#send-btn:disabled { background: var(--border); color: var(--muted); cursor: not-allowed; filter: none; }
#send-btn.sent { animation: send-flash 0.4s ease; }
#send-btn.sent::after {
  content: '✓'; position: absolute; top: -6px; right: -6px;
  background: #4ade80; color: #000; border-radius: 50%;
  width: 16px; height: 16px; font-size: 0.6rem;
  display: flex; align-items: center; justify-content: center;
  animation: check-pop 0.3s ease 0.1s both;
}

@keyframes send-flash {
  0% { transform: scale(1); }
  30% { transform: scale(0.85); }
  60% { transform: scale(1.08); }
  100% { transform: scale(1); }
}
@keyframes check-pop {
  0% { transform: scale(0); }
  100% { transform: scale(1); }
}

@media (prefers-reduced-motion: reduce) {
  #send-btn.sent { animation: none; }
  #send-btn.sent::after { animation: none; }
  #mic-btn.active { animation: none; }
}
</style>
