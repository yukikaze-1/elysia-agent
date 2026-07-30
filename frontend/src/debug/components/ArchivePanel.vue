<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useDebugStore } from '@/stores/debugStore'
import PaginationBar from './shared/PaginationBar.vue'
import ConfirmModal from './shared/ConfirmModal.vue'
import ExpandableText from './shared/ExpandableText.vue'

interface ArchiveEntry { _id: string; role: string; timestamp: string; parts: { content: string }[]; content: unknown }

const entries = ref<ArchiveEntry[]>([])
const total = ref(0)
const loading = ref(false); const error = ref('')
const search = ref(''); const roleFilter = ref('')
const selectedIds = ref<Set<string>>(new Set())
const editingId = ref<string | null>(null); const editText = ref('')
const confirmVisible = ref(false); const confirmMsg = ref(''); const pendingConfirm = ref<(() => Promise<void>) | null>(null)
const perPage = ref(100)
const store = useDebugStore()

// 300ms 防抖 — 避免每次按键都触发 API 请求
let debounceTimer: ReturnType<typeof setTimeout> | null = null

function debouncedLoad() {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    store.archivePage.offset = 0
    load()
  }, 300)
}

async function load() {
  loading.value = true; error.value = ''
  try {
    const params = new URLSearchParams({ limit: String(perPage.value), skip: String(store.archivePage.offset) })
    if (search.value) params.set('search', search.value)
    if (roleFilter.value) params.set('role', roleFilter.value)
    const res = await fetch(`/debug/api/archive?${params}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json(); entries.value = data.messages; total.value = data.total
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

onMounted(load)
watch([search, roleFilter], debouncedLoad)

function onPageChange(newOffset: number) { store.archivePage.offset = newOffset; load() }

function toggleSelect(id: string) { const s = new Set(selectedIds.value); s.has(id) ? s.delete(id) : s.add(id); selectedIds.value = s }
function toggleAll() { if (selectedIds.value.size === entries.value.length) selectedIds.value = new Set(); else selectedIds.value = new Set(entries.value.map(e => e._id)) }

async function deleteEntry(id: string) { try { await fetch(`/debug/api/archive/${id}`, { method: 'DELETE' }); load() } catch (e) { error.value = '删除失败: ' + (e as Error).message } }
async function deleteSelected() { try { for (const id of selectedIds.value) await fetch(`/debug/api/archive/${id}`, { method: 'DELETE' }); selectedIds.value = new Set(); load() } catch (e) { error.value = '批量删除失败: ' + (e as Error).message } }
async function clearAll() { try { await fetch('/debug/api/archive', { method: 'DELETE' }); load() } catch (e) { error.value = '清空失败: ' + (e as Error).message } }

function startEdit(e: ArchiveEntry) { editingId.value = e._id; editText.value = (e.parts || []).map(p => p.content).join(' ') }
async function saveEdit(id: string) { try { await fetch(`/debug/api/archive/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: editText.value }) }); editingId.value = null; load() } catch (e) { error.value = '保存失败: ' + (e as Error).message } }

function roleColor(r: string) { const m: Record<string, string> = { user: '#6af', assistant: '#f6a', tool: '#fa6', system: '#888' }; return m[r] || '#888' }
function confirmThen(fn: () => Promise<void>, msg: string) { confirmMsg.value = msg; confirmVisible.value = true; pendingConfirm.value = fn }
async function onConfirm() { confirmVisible.value = false; await pendingConfirm.value?.() }
</script>

<template>
  <div class="panel">
    <h2>Archive (MongoDB) {{ total ? `(${total})` : '' }}</h2>

    <!-- Toolbar -->
    <div class="toolbar">
      <input v-model="search" placeholder="搜索..." class="field" @keydown.enter="load" />
      <select v-model="roleFilter" class="field">
        <option value="">全部角色</option>
        <option value="user">user</option>
        <option value="assistant">assistant</option>
        <option value="tool">tool</option>
        <option value="system">system</option>
      </select>
      <button class="btn" @click="confirmThen(clearAll, '清空全部归档消息？此操作不可恢复。')">清空全部</button>
    </div>

    <!-- States -->
    <div v-if="loading" class="status">加载中...</div>
    <div v-else-if="error" class="status error">{{ error }} <button class="btn" @click="load">重试</button></div>
    <div v-else-if="entries.length === 0" class="status">{{ search || roleFilter ? '当前筛选条件下无结果' : '暂无归档消息' }}</div>

    <!-- List -->
    <div v-else>
      <div class="list-actions">
        <label><input type="checkbox" :checked="selectedIds.size === entries.length" @change="toggleAll" /> 全选</label>
        <button v-if="selectedIds.size" class="btn danger" @click="confirmThen(deleteSelected, `删除选中的 ${selectedIds.size} 条消息？`)">删除选中 ({{ selectedIds.size }})</button>
      </div>
      <div v-for="e in entries" :key="e._id" class="list-row">
        <input type="checkbox" :checked="selectedIds.has(e._id)" @change="toggleSelect(e._id)" />
        <span class="role-tag" :style="{ background: roleColor(e.role) }">{{ e.role }}</span>
        <span v-if="editingId !== e._id" class="row-text">
          <ExpandableText :text="(e.parts || []).map(p => p.content).join(' ')" :maxLength="100" />
        </span>
        <input v-else v-model="editText" class="field flex-1" @keydown.enter="saveEdit(e._id)" />
        <span class="row-meta">{{ e.timestamp?.slice(0, 16) || '' }}</span>
        <button v-if="editingId !== e._id" class="btn-sm" @click="startEdit(e)">✎</button>
        <button v-else class="btn-sm" @click="saveEdit(e._id)">✓</button>
        <button v-if="editingId !== e._id" class="btn-sm" @click="confirmThen(() => deleteEntry(e._id), '删除这条消息？')">✕</button>
        <button v-else class="btn-sm" @click="editingId = null">✕</button>
      </div>
      <PaginationBar :total="total" :perPage="perPage" :currentOffset="store.archivePage.offset" @goToOffset="onPageChange" @update:perPage="p => { perPage = p; store.archivePage.limit = p; onPageChange(0) }" />
    </div>

    <ConfirmModal :visible="confirmVisible" :message="confirmMsg" @confirm="onConfirm" @cancel="confirmVisible = false" />
  </div>
</template>

<style scoped>
.panel { background: var(--debug-surface); border: 1px solid var(--debug-border); border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.panel h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 1px; color: var(--debug-accent); margin: 0 0 10px; border-bottom: 1px solid var(--debug-border); padding-bottom: 6px; }
.toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
.field { background: var(--debug-bg); color: var(--debug-text); border: 1px solid var(--debug-border); border-radius: 4px; padding: 4px 8px; font-size: 12px; }
.field.flex-1 { flex: 1; }
.btn { padding: 4px 12px; border: 1px solid var(--debug-border); border-radius: 4px; background: transparent; color: var(--debug-text); cursor: pointer; font-size: 12px; }
.btn:hover { border-color: var(--debug-accent); }
.btn.primary { background: var(--debug-accent); color: #fff; border-color: var(--debug-accent); }
.btn.danger { color: var(--debug-red); border-color: var(--debug-red); }
.btn-sm { padding: 1px 6px; border: 1px solid var(--debug-border); border-radius: 3px; background: transparent; color: var(--debug-muted); cursor: pointer; font-size: 11px; }
.btn-sm:hover { color: var(--debug-text); }
.status { color: var(--debug-muted); padding: 12px 0; text-align: center; font-size: 13px; }
.status.error { color: var(--debug-red); }
.list-actions { display: flex; gap: 12px; align-items: center; margin-bottom: 6px; font-size: 12px; color: var(--debug-muted); }
.list-row { display: flex; gap: 8px; align-items: center; padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.04); font-size: 12px; }
.role-tag { font-size: 10px; padding: 1px 6px; border-radius: 3px; color: #fff; white-space: nowrap; font-weight: 600; min-width: 60px; text-align: center; }
.row-text { flex: 1; min-width: 0; }
.row-meta { font-size: 10px; color: var(--debug-muted); white-space: nowrap; }
</style>
