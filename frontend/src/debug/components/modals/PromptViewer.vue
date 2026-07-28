<script setup lang="ts">
import { ref, watch, onMounted, nextTick } from 'vue'

const props = defineProps<{ index: number; visible: boolean }>()
const emit = defineEmits<{ close: []; nav: [delta: number] }>()
const overlayRef = ref<HTMLElement | null>(null)

interface ToolItem { function?: { name?: string; description?: string }; name?: string; description?: string }
interface PromptDetail { index: number; timestamp: string; round_num: number; model: string; messages: { role: string; content: unknown; [key: string]: unknown }[]; tools: ToolItem[]; template_source?: { path: string; sections: Record<string, string> } }
const detail = ref<PromptDetail | null>(null)
const loading = ref(false)
const error = ref('')
const expandedMsgs = ref<Set<number>>(new Set([0])) // system prompt expanded by default
const expandedSections = ref<Set<string>>(new Set())

async function fetchPrompt() {
  if (!props.visible) return
  loading.value = true; error.value = ''
  try {
    const res = await fetch(`/debug/api/prompts/${props.index}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    detail.value = await res.json()
    expandedMsgs.value = new Set([0])
    expandedSections.value = new Set()
  } catch (e) { error.value = (e as Error).message; detail.value = null }
  finally { loading.value = false }
}

watch(() => [props.index, props.visible], () => { if (props.visible) { fetchPrompt(); nextTick(() => overlayRef.value?.focus()) } }, { immediate: true })

onMounted(() => { if (props.visible) nextTick(() => overlayRef.value?.focus()) })

function toggleMsg(i: number) {
  const s = new Set(expandedMsgs.value)
  s.has(i) ? s.delete(i) : s.add(i)
  expandedMsgs.value = s
}

function toggleSection(name: string) {
  const s = new Set(expandedSections.value)
  s.has(name) ? s.delete(name) : s.add(name)
  expandedSections.value = s
}

async function copyText(text: string) {
  try { await navigator.clipboard.writeText(text) } catch { /* */ }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'ArrowLeft') emit('nav', -1)
  else if (e.key === 'ArrowRight') emit('nav', 1)
  else if (e.key === 'Escape') emit('close')
}
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" ref="overlayRef" class="modal-overlay" @click.self="emit('close')" @keydown="onKeydown" tabindex="0">
      <div class="modal-box prompt-box">
        <div class="modal-header">
          <h3>Prompt #{{ index }}</h3>
          <button class="modal-close" @click="emit('close')">&times;</button>
        </div>
        <div class="modal-nav">
          <button @click="emit('nav', -1)">←</button>
          <span v-if="detail">Round {{ detail.round_num }} | {{ detail.model }} | {{ detail.timestamp }}</span>
          <button @click="emit('nav', 1)">→</button>
        </div>
        <div v-if="loading" class="modal-loading">加载中...</div>
        <div v-else-if="error" class="modal-error">{{ error }}</div>
        <div v-else-if="detail" class="prompt-body">
          <div class="prompt-pane">
            <h4>Messages</h4>
            <div v-for="(m, i) in detail.messages" :key="i" class="msg-item">
              <div class="msg-role" @click="toggleMsg(i)">
                <span class="role-tag" :class="m.role">{{ m.role }}</span>
                <span class="msg-size">{{ JSON.stringify(m.content).length }} chars</span>
                <button class="copy-sm" @click.stop="copyText(JSON.stringify(m.content, null, 2))">📋</button>
              </div>
              <pre v-if="expandedMsgs.has(i)" class="msg-content">{{ JSON.stringify(m.content, null, 2) }}</pre>
            </div>
          </div>
          <div v-if="detail.template_source" class="prompt-pane">
            <h4>Sections</h4>
            <button class="copy-all-btn" @click="copyText(JSON.stringify(detail.template_source.sections, null, 2))">📋 Copy All</button>
            <div v-for="(content, name) in detail.template_source.sections" :key="name" class="section-item">
              <div class="section-name" @click="toggleSection(name)">{{ name }}</div>
              <pre v-if="expandedSections.has(name)" class="section-content">{{ content }}</pre>
            </div>
          </div>
          <div v-if="detail.tools && detail.tools.length" class="prompt-pane">
            <h4>Tools ({{ detail.tools.length }})</h4>
            <button class="copy-all-btn" @click="copyText(JSON.stringify(detail.tools, null, 2))">📋 Copy JSON</button>
            <div v-for="(t, i) in detail.tools" :key="i" class="tool-card-sm">
              <strong>{{ t.function?.name || t.name }}</strong>
              <p>{{ t.function?.description || t.description }}</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.modal-overlay { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; }
.modal-box { background: var(--debug-surface); border: 1px solid var(--debug-border); border-radius: 8px; padding: 20px; max-width: 700px; width: 90vw; max-height: 80vh; overflow-y: auto; box-shadow: 0 4px 24px rgba(0,0,0,0.5); }
.prompt-box { max-width: 960px; }
.modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.modal-header h3 { margin: 0; font-size: 15px; }
.modal-close { background: none; border: none; color: var(--debug-muted); font-size: 22px; cursor: pointer; }
.modal-nav { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; font-size: 12px; color: var(--debug-muted); }
.modal-nav button { padding: 2px 10px; border: 1px solid var(--debug-border); border-radius: 4px; background: transparent; color: var(--debug-text); cursor: pointer; }
.modal-loading, .modal-error { color: var(--debug-muted); padding: 20px; text-align: center; }
.modal-error { color: var(--debug-red); }
.prompt-body { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.prompt-pane { overflow: hidden; }
.prompt-pane h4 { font-size: 13px; color: var(--debug-accent); margin: 0 0 8px; border-bottom: 1px solid var(--debug-border); padding-bottom: 4px; }
.msg-item { margin-bottom: 4px; }
.msg-role { cursor: pointer; display: flex; align-items: center; gap: 8px; padding: 4px 0; }
.role-tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; text-transform: uppercase; }
.role-tag.system { background: #2a1a3a; color: #f6a; }
.role-tag.user { background: #1a3a5c; color: #6af; }
.role-tag.assistant { background: #1a2e1a; color: #6f6; }
.role-tag.tool { background: #3a2a1a; color: #fa6; }
.msg-size { font-size: 10px; color: var(--debug-muted); }
.copy-sm { font-size: 10px; background: none; border: 1px solid var(--debug-border); border-radius: 2px; cursor: pointer; padding: 0 4px; }
.msg-content, .section-content { font-size: 11px; white-space: pre-wrap; word-break: break-all; background: var(--debug-bg); border-radius: 4px; padding: 8px; max-height: 200px; overflow-y: auto; }
.section-name { cursor: pointer; font-size: 12px; padding: 3px 0; color: var(--debug-text); }
.section-name:hover { color: var(--debug-accent); }
.copy-all-btn { font-size: 11px; margin-bottom: 8px; padding: 2px 8px; border: 1px solid var(--debug-border); border-radius: 4px; background: transparent; color: var(--debug-muted); cursor: pointer; }
.tool-card-sm { font-size: 11px; padding: 6px; margin-bottom: 4px; border: 1px solid var(--debug-border); border-radius: 4px; }
.tool-card-sm strong { color: var(--debug-accent); }
.tool-card-sm p { margin: 2px 0 0; color: var(--debug-muted); font-size: 11px; }
</style>
