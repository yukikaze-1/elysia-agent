<script setup lang="ts">
import { ref, watch, onMounted, nextTick } from 'vue'
import { useAbortController } from '@/composables/useAbortController'
import { debugFetch } from '../../auth'

const props = defineProps<{ index: number; visible: boolean }>()
const emit = defineEmits<{ close: []; nav: [delta: number] }>()
const { signal } = useAbortController()
const overlayRef = ref<HTMLElement | null>(null)

interface ResponseDetail { index: number; timestamp: string; round_num: number; raw_text: string; inner_voice: string; text_len: number; inner_voice_len: number }
const detail = ref<ResponseDetail | null>(null)
const loading = ref(false)
const error = ref('')

async function fetchResponse() {
  if (!props.visible) return
  loading.value = true; error.value = ''
  try {
    const res = await debugFetch(`/debug/api/responses/${props.index}`, { signal: signal() })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    detail.value = await res.json()
  } catch (e) { if ((e as Error).name === 'AbortError') return; error.value = (e as Error).message; detail.value = null }
  finally { loading.value = false }
}

watch(() => [props.index, props.visible], () => { if (props.visible) { fetchResponse(); nextTick(() => overlayRef.value?.focus()) } }, { immediate: true })

onMounted(() => { if (props.visible) nextTick(() => overlayRef.value?.focus()) })

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'ArrowLeft') emit('nav', -1)
  else if (e.key === 'ArrowRight') emit('nav', 1)
  else if (e.key === 'Escape') emit('close')
}
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" ref="overlayRef" class="modal-overlay" @click.self="emit('close')" @keydown="onKeydown" tabindex="0">
      <div class="modal-box wide">
        <div class="modal-header">
          <h3>Response #{{ index }}</h3>
          <button class="modal-close" @click="emit('close')">&times;</button>
        </div>
        <div class="modal-nav">
          <button @click="emit('nav', -1)">←</button>
          <span v-if="detail">Round {{ detail.round_num }} | {{ detail.timestamp }}</span>
          <button @click="emit('nav', 1)">→</button>
        </div>
        <div v-if="loading" class="modal-loading">加载中...</div>
        <div v-else-if="error" class="modal-error">{{ error }}</div>
        <div v-else-if="detail" class="response-body">
          <div v-if="detail.inner_voice" class="inner-voice">{{ detail.inner_voice }}</div>
          <pre class="raw-text">{{ detail.raw_text }}</pre>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.modal-overlay { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; }
.modal-box { background: var(--debug-surface); border: 1px solid var(--debug-border); border-radius: 8px; padding: 20px; max-width: 700px; width: 90vw; max-height: 80vh; overflow-y: auto; box-shadow: 0 4px 24px rgba(0,0,0,0.5); }
.modal-box.wide { max-width: 900px; }
.modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.modal-header h3 { margin: 0; font-size: 15px; }
.modal-close { background: none; border: none; color: var(--debug-muted); font-size: 22px; cursor: pointer; }
.modal-nav { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; font-size: 12px; color: var(--debug-muted); }
.modal-nav button { padding: 2px 10px; border: 1px solid var(--debug-border); border-radius: 4px; background: transparent; color: var(--debug-text); cursor: pointer; }
.modal-loading, .modal-error { color: var(--debug-muted); padding: 20px; text-align: center; }
.modal-error { color: var(--debug-red); }
.inner-voice { color: var(--debug-muted); font-style: italic; font-size: 13px; border-left: 2px solid var(--debug-border); padding: 4px 12px; margin-bottom: 12px; }
.raw-text { white-space: pre-wrap; word-break: break-all; font-size: 12px; line-height: 1.5; max-height: 50vh; overflow-y: auto; }
</style>
