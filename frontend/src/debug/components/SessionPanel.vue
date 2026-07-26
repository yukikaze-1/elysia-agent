<script setup lang="ts">
import { ref, onMounted } from 'vue'
import ConfirmModal from './shared/ConfirmModal.vue'
import ExpandableText from './shared/ExpandableText.vue'

interface SessionItem { session_id: string; message_count: number; updated_at: string; token_total: number }
interface SessionDetail { session_id: string; messages: { role: string; timestamp: string; content: unknown; content_type?: Record<string, unknown>; parts: { content: string }[] }[]; params?: unknown; total?: number }

const sessions = ref<SessionItem[]>([])
const loading = ref(false)
const error = ref('')
const selectedIds = ref<Set<string>>(new Set())
const expandingId = ref<string | null>(null)
const detail = ref<SessionDetail | null>(null)
const detailLoading = ref(false)

const confirmVisible = ref(false)
const confirmMsg = ref('')
const pendingConfirm = ref<(() => Promise<void>) | null>(null)

async function load() {
  loading.value = true; error.value = ''
  try {
    const res = await fetch('/debug/api/sessions')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    sessions.value = data.sessions || []
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

onMounted(load)

function toggleSelect(id: string) {
  const s = new Set(selectedIds.value); s.has(id) ? s.delete(id) : s.add(id); selectedIds.value = s
}
function toggleAll() {
  if (selectedIds.value.size === sessions.value.length) selectedIds.value = new Set()
  else selectedIds.value = new Set(sessions.value.map(s => s.session_id))
}

async function viewDetail(sessionId: string) {
  if (expandingId.value === sessionId) { expandingId.value = null; return }
  expandingId.value = sessionId; detailLoading.value = true
  try {
    const res = await fetch(`/debug/api/sessions/${sessionId}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    detail.value = await res.json()
  } catch { detail.value = null }
  finally { detailLoading.value = false }
}

async function deleteSessions(ids: string[]) {
  for (const id of ids) { await fetch(`/debug/api/sessions/${id}`, { method: 'DELETE' }) }
  selectedIds.value = new Set(); load()
}

async function clearAll() {
  await fetch('/debug/api/sessions', { method: 'DELETE' }); load()
}

function confirmThen(fn: () => Promise<void>, msg: string) {
  confirmMsg.value = msg; confirmVisible.value = true
  pendingConfirm.value = fn
}
async function onConfirm() { confirmVisible.value = false; await pendingConfirm.value?.() }

function roleColor(r: string) { const m: Record<string, string> = { user: '#6af', assistant: '#f6a', tool: '#fa6', system: '#888' }; return m[r] || '#888' }
</script>

<template>
  <div class="panel">
    <h2>Sessions (MongoDB) {{ sessions.length ? `(${sessions.length})` : '' }}</h2>
    <div class="toolbar">
      <button class="btn" @click="confirmThen(clearAll, '清空全部 session？')">清空全部</button>
    </div>

    <div v-if="loading" class="status">加载中...</div>
    <div v-else-if="error" class="status error">{{ error }} <button class="btn" @click="load">重试</button></div>
    <div v-else-if="sessions.length === 0" class="status">暂无会话</div>

    <div v-else>
      <div class="list-actions">
        <label><input type="checkbox" :checked="selectedIds.size === sessions.length" @change="toggleAll" /> 全选</label>
        <button v-if="selectedIds.size" class="btn danger" @click="confirmThen(() => deleteSessions([...selectedIds]), `删除选中的 ${selectedIds.size} 个 session？`)">删除选中 ({{ selectedIds.size }})</button>
      </div>
      <div v-for="s in sessions" :key="s.session_id" class="list-row">
        <input type="checkbox" :checked="selectedIds.has(s.session_id)" @change="toggleSelect(s.session_id)" />
        <span class="row-text mono">{{ s.session_id?.slice(0, 12) }}...</span>
        <span class="row-meta">{{ s.message_count }} msgs · {{ s.token_total }} tokens · {{ s.updated_at?.slice(0, 16) || '' }}</span>
        <button class="btn-sm" @click="viewDetail(s.session_id)">{{ expandingId === s.session_id ? '收起' : '详情' }}</button>
        <button class="btn-sm" @click="confirmThen(() => deleteSessions([s.session_id]), '删除此 session？')">✕</button>
      </div>

      <!-- Detail expand -->
      <div v-if="expandingId && detailLoading" class="status">加载详情中...</div>
      <div v-else-if="expandingId && detail" class="detail-block">
        <h4>Session {{ detail.session_id }}</h4>
        <div v-for="(m, i) in detail.messages" :key="i" class="detail-msg">
          <span class="role-tag" :style="{ background: roleColor(m.role) }">{{ m.role }}</span>
          <ExpandableText :text="(m.parts || []).map(p => p.content).join(' ')" :maxLength="80" />
        </div>
      </div>
    </div>
    <ConfirmModal :visible="confirmVisible" :message="confirmMsg" @confirm="onConfirm" @cancel="confirmVisible = false" />
  </div>
</template>

<style scoped>
.panel { background: var(--debug-surface); border: 1px solid var(--debug-border); border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.panel h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 1px; color: var(--debug-accent); margin: 0 0 10px; border-bottom: 1px solid var(--debug-border); padding-bottom: 6px; }
.toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
.btn { padding: 4px 12px; border: 1px solid var(--debug-border); border-radius: 4px; background: transparent; color: var(--debug-text); cursor: pointer; font-size: 12px; }
.btn:hover { border-color: var(--debug-accent); }
.btn.danger { color: var(--debug-red); border-color: var(--debug-red); }
.btn-sm { padding: 1px 6px; border: 1px solid var(--debug-border); border-radius: 3px; background: transparent; color: var(--debug-muted); cursor: pointer; font-size: 11px; }
.btn-sm:hover { color: var(--debug-text); }
.status { color: var(--debug-muted); padding: 12px 0; text-align: center; font-size: 13px; }
.status.error { color: var(--debug-red); }
.list-actions { display: flex; gap: 12px; align-items: center; margin-bottom: 6px; font-size: 12px; color: var(--debug-muted); }
.list-row { display: flex; gap: 8px; align-items: center; padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.04); font-size: 12px; }
.row-text { flex: 1; min-width: 0; }
.row-text.mono { font-family: monospace; font-size: 11px; }
.row-meta { font-size: 10px; color: var(--debug-muted); white-space: nowrap; }
.detail-block { margin-top: 12px; padding: 10px; background: var(--debug-bg); border-radius: 6px; }
.detail-block h4 { font-size: 12px; color: var(--debug-accent); margin: 0 0 8px; }
.detail-msg { display: flex; gap: 6px; align-items: baseline; padding: 2px 0; font-size: 11px; }
.role-tag { font-size: 9px; padding: 1px 5px; border-radius: 3px; color: #fff; font-weight: 600; white-space: nowrap; }
</style>
