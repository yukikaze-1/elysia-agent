<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useDebugStore } from '@/stores/debugStore'
import PaginationBar from './shared/PaginationBar.vue'
import ConfirmModal from './shared/ConfirmModal.vue'
import ExpandableText from './shared/ExpandableText.vue'

interface LtmEntry { id: string; text: string; memory_type: string; poignancy: number; timestamp: string | number; metadata?: Record<string, unknown> }

const store = useDebugStore()

const entries = ref<LtmEntry[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const search = ref('')
const typeFilter = ref('')
const selectedIds = ref<Set<string>>(new Set())

// 300ms 防抖 — 避免每次按键都触发 API 请求
let debounceTimer: ReturnType<typeof setTimeout> | null = null

function debouncedLoad() {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    store.ltmPage.offset = 0
    load()
  }, 300)
}
const editingId = ref<string | null>(null)
const editText = ref('')

// Confirm state
const confirmVisible = ref(false)
const confirmMsg = ref('')
const pendingConfirm = ref<(() => Promise<void>) | null>(null)

// Create modal
const createVisible = ref(false)
const createText = ref('')
const createType = ref('note')
const createPoignancy = ref(5)

const perPage = ref(50)

function fmtTs(ts: string | number): string {
  if (!ts) return ''
  if (typeof ts === 'number') return new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ')
  return ts.slice(0, 16)
}

async function load() {
  loading.value = true; error.value = ''
  try {
    const params = new URLSearchParams({ limit: String(perPage.value), offset: String(store.ltmPage.offset) })
    if (search.value) params.set('search', search.value)
    if (typeFilter.value) params.set('type', typeFilter.value)
    const res = await fetch(`/debug/api/ltm?${params}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    entries.value = data.entries; total.value = data.total
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

onMounted(load)
watch([search, typeFilter], debouncedLoad)

function onPageChange(newOffset: number) { store.ltmPage.offset = newOffset; load() }

function toggleSelect(id: string) {
  const s = new Set(selectedIds.value)
  s.has(id) ? s.delete(id) : s.add(id)
  selectedIds.value = s
}

function toggleAll() {
  if (selectedIds.value.size === entries.value.length) selectedIds.value = new Set()
  else selectedIds.value = new Set(entries.value.map(e => e.id))
}

async function deleteEntry(id: string) {
  try {
    await fetch(`/debug/api/ltm/${id}`, { method: 'DELETE' })
    load()
  } catch (e) { error.value = '删除失败: ' + (e as Error).message }
}

async function deleteSelected() {
  try {
    for (const id of selectedIds.value) { await fetch(`/debug/api/ltm/${id}`, { method: 'DELETE' }) }
    selectedIds.value = new Set()
    load()
  } catch (e) { error.value = '批量删除失败: ' + (e as Error).message }
}

async function clearAll() {
  try {
    await fetch('/debug/api/ltm', { method: 'DELETE' })
    load()
  } catch (e) { error.value = '清空失败: ' + (e as Error).message }
}

function startEdit(entry: LtmEntry) { editingId.value = entry.id; editText.value = entry.text }

async function saveEdit(id: string) {
  try {
    await fetch(`/debug/api/ltm/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: editText.value }) })
    editingId.value = null
    load()
  } catch (e) { error.value = '保存失败: ' + (e as Error).message }
}

async function createEntry() {
  try {
    await fetch('/debug/api/ltm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: createText.value, memory_type: createType.value, poignancy: createPoignancy.value }) })
    createVisible.value = false; createText.value = ''; load()
  } catch (e) { error.value = '创建失败: ' + (e as Error).message }
}

function memoryColor(t: string) {
  const m: Record<string, string> = { fact: '#4caf50', plan: '#2196f3', preference: '#9c27b0', event: '#ff9800' }
  return m[t.toLowerCase()] || '#888'
}

function confirmThen(fn: () => Promise<void>, msg: string) {
  confirmMsg.value = msg; confirmVisible.value = true
  pendingConfirm.value = fn
}

async function onConfirm() {
  confirmVisible.value = false
  await pendingConfirm.value?.()
}
</script>

<template>
  <div class="panel">
    <h2>Long-Term Memory (Milvus) {{ total ? `(${total})` : '' }}</h2>

    <!-- Toolbar -->
    <div class="toolbar">
      <input v-model="search" placeholder="搜索..." class="field" @keydown.enter="load" />
      <select v-model="typeFilter" class="field">
        <option value="">全部类型</option>
        <option value="fact">Fact</option>
        <option value="plan">Plan</option>
        <option value="preference">Preference</option>
        <option value="event">Event</option>
        <option value="note">Note</option>
      </select>
      <button class="btn" @click="createVisible = true">+ 添加</button>
      <button class="btn" @click="confirmThen(clearAll, '清空全部 LTM 记忆？此操作不可恢复。')">清空全部</button>
    </div>

    <!-- States -->
    <div v-if="loading" class="status">加载中...</div>
    <div v-else-if="error" class="status error">{{ error }} <button class="btn" @click="load">重试</button></div>
    <div v-else-if="entries.length === 0" class="status">{{ search || typeFilter ? '当前筛选条件下无结果' : '暂无记忆数据' }}</div>

    <!-- List -->
    <div v-else>
      <div class="list-actions">
        <label><input type="checkbox" :checked="selectedIds.size === entries.length" @change="toggleAll" /> 全选</label>
        <button v-if="selectedIds.size" class="btn danger" @click="confirmThen(deleteSelected, `删除选中的 ${selectedIds.size} 条记忆？`)">删除选中 ({{ selectedIds.size }})</button>
      </div>
      <div v-for="e in entries" :key="e.id" class="list-row">
        <input type="checkbox" :checked="selectedIds.has(e.id)" @change="toggleSelect(e.id)" />
        <span class="type-tag" :style="{ background: memoryColor(e.memory_type) }">{{ e.memory_type }}</span>
        <span v-if="editingId !== e.id" class="row-text">
          <ExpandableText :text="e.text" :maxLength="100" />
        </span>
        <input v-else v-model="editText" class="field flex-1" @keydown.enter="saveEdit(e.id)" />
        <span class="row-meta">{{ e.poignancy }} · {{ fmtTs(e.timestamp) }}</span>
        <button v-if="editingId !== e.id" class="btn-sm" @click="startEdit(e)">✎</button>
        <button v-else class="btn-sm" @click="saveEdit(e.id)">✓</button>
        <button v-if="editingId !== e.id" class="btn-sm" @click="confirmThen(() => deleteEntry(e.id), '删除这条记忆？')">✕</button>
        <button v-else class="btn-sm" @click="editingId = null">✕</button>
      </div>
      <PaginationBar :total="total" :perPage="perPage" :currentOffset="store.ltmPage.offset" @goToOffset="onPageChange" @update:perPage="p => { perPage = p; store.ltmPage.limit = p; onPageChange(0) }" />
    </div>

    <!-- Create Modal -->
    <Teleport to="body">
      <div v-if="createVisible" class="modal-overlay" @click.self="createVisible = false">
        <div class="modal-box">
          <h3>添加新记忆</h3>
          <label class="field-label">内容</label>
          <textarea v-model="createText" class="field" rows="4" style="width:100%" />
          <label class="field-label">类型</label>
          <select v-model="createType" class="field">
            <option value="fact">Fact</option>
            <option value="plan">Plan</option>
            <option value="preference">Preference</option>
            <option value="event">Event</option>
            <option value="note">Note</option>
          </select>
          <label class="field-label">Poignancy: {{ createPoignancy }}</label>
          <input v-model.number="createPoignancy" type="range" min="1" max="10" class="field" style="width:100%" />
          <div class="modal-actions">
            <button class="btn" @click="createVisible = false">取消</button>
            <button class="btn primary" @click="createEntry">保存</button>
          </div>
        </div>
      </div>
    </Teleport>

    <ConfirmModal :visible="confirmVisible" :message="confirmMsg" @confirm="onConfirm" @cancel="confirmVisible = false" />
  </div>
</template>

<style>
@import '@/debug/styles/panel.css';
</style>

<style scoped>
.type-tag { font-size: 10px; padding: 1px 6px; border-radius: 3px; color: #fff; white-space: nowrap; font-weight: 600; min-width: 60px; text-align: center; }
.field-label { display: block; color: var(--debug-muted); font-size: 11px; margin: 8px 0 3px; }
.modal-overlay { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center; }
.modal-box { background: var(--debug-surface); border: 1px solid var(--debug-border); border-radius: 8px; padding: 20px; min-width: 360px; max-width: 500px; box-shadow: 0 4px 20px rgba(0,0,0,0.4); }
.modal-box h3 { margin: 0 0 12px; font-size: 15px; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
</style>
