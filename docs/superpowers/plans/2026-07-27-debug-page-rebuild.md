# Debug Page 完整重建 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 debug 仪表盘从 122 行骨架恢复到完整的 3 Tab（16 面板 + DB CRUD + SSE 任务流）+ 4 Modal 查看器

**Architecture:** Pinia store 管理轮询生命周期 + activeTab 切换。Overview 16 面板 inline 渲染。Database 三个子面板独立 fetch API。Tasks 用 EventSource SSE + vis-network DAG。所有 modal Teleport to body。

**Tech Stack:** Vue 3 + Pinia + TypeScript + vis-network + vis-data + native fetch/EventSource/Clipboard API

**Spec:** `docs/superpowers/specs/2026-07-27-debug-page-rebuild-design.md`

## Global Constraints

- 所有组件用 `<script setup lang="ts">` + `<style scoped>`
- CSS 变量在 App.vue 全局 `<style>` 块定义（`--debug-bg`, `--debug-surface`, `--debug-border`, `--debug-text`, `--debug-muted`, `--debug-accent`, `--debug-green`, `--debug-warn`, `--debug-red`）
- 分页统一用 `offset`；ArchivePanel 调用 API 时映射 `offset` → `skip`
- Destructive ops 全部经 ConfirmModal 确认
- 内存类型色码：Fact=#4caf50, Plan=#2196f3, Preference=#9c27b0, Event=#ff9800, Note=#888
- 任务状态色码：running=#42a5f5, completed=#66bb6a, failed=#ef5350, cancelled=#bdbdbd

---

### Task 1: Foundation — install deps + types + main.ts

**Files:**
- Create: `frontend/src/debug/types.ts`
- Modify: `frontend/src/debug/main.ts`

**Interfaces:**
- Produces: `DebugData`, `TokenRoundItem`, `TokenStats`, `TimingStats`, `ProcessMemory`, `PromptRecord`, `ResponseRecord`, `SessionRecentItem`, `ContentPart`, `LtmRecentItem`, `ToolItem`, `ToolHistoryItem`, `ConsolidationHistoryItem`, `ConsolidationInfo`, `MemoryReflectorInfo`, `CharacterReflectorInfo`, `CharacterEngineInfo` — all exported from `@/debug/types`

- [ ] **Step 1: Install vis-network + vis-data**

```bash
cd frontend && npm install vis-network vis-data
```

- [ ] **Step 2: Create `frontend/src/debug/types.ts`**

```ts
// ---- 子结构 ----

export interface TokenRoundItem {
  model: string; prompt_tokens: number; completion_tokens: number
  total_tokens: number; round_num: number
}

export interface TokenStats {
  session_total: number; per_model: Record<string, number>
  current_turn: TokenRoundItem[]
}

export interface TimingStats {
  count: number; avg_ms: number; p50_ms: number; p95_ms: number; p99_ms: number; last_ms: number
}

/** 进程内存（rss/vms/pct/threads），不是"长期记忆" */
export interface ProcessMemory {
  rss_mb: number; vms_mb: number; pct: number; num_threads: number
}

export interface PromptRecord {
  index: number; timestamp: string; round_num: number
  message_count: number; system_prompt_len: number; total_chars: number; model: string
}

export interface ResponseRecord {
  index: number; timestamp: string; round_num: number; text_len: number; inner_voice_len: number
}

export interface ContentPart {
  type: string; content?: string; src?: string; mime?: string
}

export interface SessionRecentItem {
  role: string; parts: { content: string }[]; content: ContentPart[]
  content_type?: Record<string, unknown>
}

export interface LtmRecentItem {
  id: string; text: string; memory_type: string; poignancy: number
  metadata?: Record<string, unknown>
}

export interface ToolItem {
  name: string; description: string; parameters?: Record<string, unknown>
}

export interface ToolHistoryItem {
  name: string; args?: unknown; result?: unknown; error?: string
}

export interface ConsolidationHistoryItem {
  type: string; content?: string; [key: string]: unknown
}

// ---- 分组子对象（面板 props 类型标注） ----

export interface ConsolidationInfo {
  buffer_size: number; threshold: number; history: ConsolidationHistoryItem[]
}

export interface MemoryReflectorInfo {
  exists: boolean; round_count: number; round_threshold: number
  new_memory_count: number; memory_threshold: number
  locked: boolean; last_summary?: unknown
}

export interface CharacterReflectorInfo {
  exists: boolean; buffer_size: number; last_reflection_at?: number; locked: boolean
  self_perception_history?: unknown[]
  last_prompt_system?: string; last_prompt_user?: string
  last_text_len?: number; last_duration_ms?: number; last_success?: boolean
}

export interface CharacterEngineInfo {
  exists: boolean; self_l2: string; state_l3: string; mood: string
  last_awareness_time?: number; l2_version_count: number; c6_version_count: number
  state_history?: unknown[]
}

// ---- DebugData（与后端 DebugInfo dataclass 平铺字段一一对应） ----

export interface DebugData {
  fsm_state: string; fsm_previous: string | null; uptime_seconds: number; processing: boolean
  llm_model: string; tts_ok: boolean; stt_ok: boolean; ws_clients: number
  proactive_enabled: boolean; proactive_interval?: number; last_interaction_ago?: number
  session_messages: number; session_recent: SessionRecentItem[]
  character_state_raw: Record<string, unknown>
  character_name: string; base_personality_summary: string
  archive_msg_count: number; lt_count: number; lt_recent: LtmRecentItem[]
  milvus_ok: boolean; embedding_ok: boolean; mongodb_ok: boolean
  currently_executing: string[]; tools: ToolItem[]; tool_history: ToolHistoryItem[]
  consolidator_buffer_size: number; consolidator_threshold: number
  consolidation_history: ConsolidationHistoryItem[]
  mreflector_exists: boolean; mreflector_round_count: number; mreflector_round_threshold: number
  mreflector_new_memory_count: number; mreflector_memory_threshold: number
  mreflector_locked: boolean; mreflector_last_summary: unknown
  reflector_exists: boolean; reflector_buffer_size: number
  reflector_last_reflection_at: number | null; reflector_locked: boolean
  self_perception_history: unknown[]
  reflector_last_prompt_system: string; reflector_last_prompt_user: string
  reflector_last_success: boolean; reflector_last_duration_ms: number; reflector_last_text_len: number
  character_engine_exists: boolean; self_l2: string; state_l3: string
  state_history: unknown[]; last_awareness_time: number
  l2_version_count: number; c6_version_count: number
  token_stats: TokenStats; pending_token_count: number; pending_session_exists: boolean
  prompt_summary: { records: PromptRecord[]; total: number; max: number }
  response_summary: { records: ResponseRecord[]; total: number; max: number }
  timing_history: unknown[]; timing_stats: TimingStats
  /** 进程内存（不是"长期记忆"） */
  memory: ProcessMemory
  mood: string
  scheduler_pending: number; scheduler_running: number; scheduler_total: number; task_tree: unknown
}
```

- [ ] **Step 3: Modify `frontend/src/debug/main.ts` — add Pinia**

```ts
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import '../styles/variables.css'

const app = createApp(App)
app.use(createPinia())
app.mount('#debug-app')
```

- [ ] **Step 4: Build to verify types**

```bash
cd frontend && npx vue-tsc --noEmit 2>&1 | head -10
```

- [ ] **Step 5: Commit**

```bash
cd frontend && npm install vis-network vis-data
git add frontend/package.json frontend/package-lock.json frontend/src/debug/types.ts frontend/src/debug/main.ts
git commit -m "feat(debug): add types, Pinia, vis-network dependency"
```

---

### Task 2: debugStore.ts

**Files:**
- Create: `frontend/src/stores/debugStore.ts`

**Interfaces:**
- Produces: `useDebugStore` — Pinia store with `data`, `activeTab`, `paused`, `ltmPage`, `archivePage`, `fetchDebugData()`, `startPoll()`, `stopPoll()`
- Consumes: `DebugData` from `@/debug/types`

- [ ] **Step 1: Create `frontend/src/stores/debugStore.ts`**

```ts
import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import type { DebugData } from '@/debug/types'

export const useDebugStore = defineStore('debug', () => {
  const data = ref<DebugData | null>(null)
  const activeTab = ref<'overview' | 'database' | 'tasks'>('overview')
  const paused = ref(false)
  const error = ref<string | null>(null)

  const ltmPage = ref({ limit: 50, offset: 0 })
  const archivePage = ref({ limit: 100, offset: 0 })

  let pollTimer: ReturnType<typeof setInterval> | null = null
  let pollInterval = 2000

  async function fetchDebugData() {
    try {
      const res = await fetch('/debug/api')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      data.value = await res.json()
      error.value = null
      pollInterval = 2000 // 成功时重置退避
    } catch (e) {
      error.value = (e as Error).message
      pollInterval = Math.min(pollInterval * 2, 30000) // 指数退避到 30s
      restartPollTimer()
    }
  }

  function restartPollTimer() {
    if (pollTimer) clearInterval(pollTimer)
    if (!paused.value) {
      pollTimer = setInterval(fetchDebugData, pollInterval)
    }
  }

  function startPoll() {
    if (paused.value) return
    fetchDebugData()
    pollTimer = setInterval(fetchDebugData, pollInterval)
  }

  function stopPoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
    pollInterval = 2000
  }

  // Tab 切换：切到 overview 开始轮询，切走停止
  watch(activeTab, (tab) => {
    if (tab === 'overview') startPoll()
    else stopPoll()
  })

  // Pause 切换
  watch(paused, (p) => {
    if (p) stopPoll()
    else startPoll()
  })

  // 初始化：默认在 overview tab，自动开始轮询
  startPoll()

  return { data, activeTab, paused, error, ltmPage, archivePage, fetchDebugData, startPoll, stopPoll }
})
```

- [ ] **Step 2: Build to verify**

```bash
cd frontend && npx vue-tsc --noEmit 2>&1 | grep -i "debugStore\|error" | head -10
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/stores/debugStore.ts
git commit -m "feat(debug): add debugStore with poll lifecycle and pagination state"
```

---

### Task 3: App.vue — shell with tabs + CSS

**Files:**
- Modify: `frontend/src/debug/App.vue` (complete rewrite)

**Interfaces:**
- Consumes: `useDebugStore` from `@/stores/debugStore`
- Produces: Shell with tab bar, pause button, copy JSON buttons, `<component :is>` dynamic tab switching

- [ ] **Step 1: Rewrite `frontend/src/debug/App.vue`**

```vue
<script setup lang="ts">
import { computed, markRaw, type Component } from 'vue'
import { useDebugStore } from '@/stores/debugStore'
import OverviewTab from './components/OverviewTab.vue'
import DatabaseTab from './components/DatabaseTab.vue'
import TasksTab from './components/TasksTab.vue'

const store = useDebugStore()

const tabs: { key: 'overview' | 'database' | 'tasks'; label: string; comp: Component }[] = [
  { key: 'overview', label: '概览', comp: markRaw(OverviewTab) },
  { key: 'database', label: '数据库', comp: markRaw(DatabaseTab) },
  { key: 'tasks', label: '任务', comp: markRaw(TasksTab) },
]

const activeComp = computed(() => tabs.find(t => t.key === store.activeTab)?.comp)

function switchTab(key: 'overview' | 'database' | 'tasks') {
  store.activeTab = key
}

function togglePause() {
  store.paused = !store.paused
}

async function copyJson(indent: number) {
  if (!store.data) return
  try {
    await navigator.clipboard.writeText(JSON.stringify(store.data, null, indent))
  } catch { /* clipboard denied */ }
}
</script>

<template>
  <div class="debug-shell">
    <!-- Top bar -->
    <header class="debug-header">
      <h1>Elysia Debug</h1>
      <nav class="tab-bar">
        <button
          v-for="t in tabs" :key="t.key"
          :class="['tab-btn', { active: store.activeTab === t.key }]"
          @click="switchTab(t.key)"
        >{{ t.label }}</button>
      </nav>
      <div class="header-actions">
        <button class="action-btn" @click="togglePause">
          {{ store.paused ? '▶ 恢复' : '⏸ 暂停' }}
        </button>
        <button class="action-btn" @click="copyJson(0)">📋 JSON</button>
        <button class="action-btn" @click="copyJson(2)">📋 JSON (light)</button>
      </div>
      <span v-if="store.error" class="poll-error">⚠ {{ store.error }}</span>
      <span v-if="store.paused" class="pause-indicator">已暂停</span>
    </header>

    <!-- Content -->
    <main class="debug-content">
      <component :is="activeComp" />
    </main>
  </div>
</template>

<style>
/* 全局 style — 覆盖 body，不影响主应用 */
:root {
  --debug-bg: #0f0f1a;
  --debug-surface: #1a1a2e;
  --debug-border: #2a2a4a;
  --debug-text: #ddd;
  --debug-muted: #888;
  --debug-accent: #e94560;
  --debug-green: #4caf50;
  --debug-warn: #ff9800;
  --debug-red: #f44336;
}

body {
  font-family: 'Segoe UI', system-ui, monospace;
  background: var(--debug-bg);
  color: var(--debug-text);
  padding: 20px;
  max-width: 1100px;
  margin: 0 auto;
  font-size: 13px;
  line-height: 1.5;
  overflow: auto;
}
</style>

<style scoped>
.debug-shell { display: flex; flex-direction: column; gap: 12px; }

.debug-header {
  display: flex; align-items: center; flex-wrap: wrap; gap: 12px;
  padding: 8px 0; border-bottom: 1px solid var(--debug-border);
}

.debug-header h1 { font-size: 18px; margin: 0; white-space: nowrap; }

.tab-bar { display: flex; gap: 4px; }

.tab-btn {
  padding: 6px 14px; border: 1px solid var(--debug-border); border-radius: 6px;
  background: transparent; color: var(--debug-muted); cursor: pointer;
  font-size: 13px; transition: all 0.2s;
}
.tab-btn:hover { color: var(--debug-text); border-color: var(--debug-accent); }
.tab-btn.active { background: var(--debug-accent); color: #fff; border-color: var(--debug-accent); }

.header-actions { display: flex; gap: 6px; margin-left: auto; }

.action-btn {
  padding: 4px 10px; border: 1px solid var(--debug-border); border-radius: 4px;
  background: transparent; color: var(--debug-muted); cursor: pointer;
  font-size: 12px; transition: all 0.2s;
}
.action-btn:hover { color: var(--debug-text); border-color: var(--debug-accent); }

.poll-error { color: var(--debug-red); font-size: 12px; }
.pause-indicator { color: var(--debug-warn); font-size: 12px; font-weight: 600; }

.debug-content { min-height: 400px; }
</style>
```

- [ ] **Step 2: Build to verify (will fail on missing component imports — expected)**

```bash
cd frontend && npx vite build 2>&1 | tail -5
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/debug/App.vue
git commit -m "feat(debug): rewrite App.vue as tab shell with pause/copy controls"
```

---

### Task 4: Shared UI components

**Files:**
- Create: `frontend/src/debug/components/shared/PaginationBar.vue`
- Create: `frontend/src/debug/components/shared/ConfirmModal.vue`
- Create: `frontend/src/debug/components/shared/ExpandableText.vue`

**Interfaces:**
- Produces:
  - `PaginationBar` — Props: `total: number`, `perPage: number`, `currentOffset: number`; Emits: `update:perPage`, `goToOffset`
  - `ConfirmModal` — Props: `message: string`, `visible: boolean`; Emits: `confirm`, `cancel`
  - `ExpandableText` — Props: `text: string`, `maxLength?: number` (default 120)

- [ ] **Step 1: Create `frontend/src/debug/components/shared/ConfirmModal.vue`**

```vue
<script setup lang="ts">
defineProps<{ message: string; visible: boolean }>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="confirm-overlay" @click.self="emit('cancel')">
      <div class="confirm-box">
        <p class="confirm-msg">{{ message }}</p>
        <div class="confirm-actions">
          <button class="confirm-btn cancel" @click="emit('cancel')">取消</button>
          <button class="confirm-btn danger" @click="emit('confirm')">确认</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.confirm-overlay {
  position: fixed; inset: 0; z-index: 9999;
  background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center;
}
.confirm-box {
  background: var(--debug-surface); border: 1px solid var(--debug-border);
  border-radius: 8px; padding: 20px 24px; min-width: 300px; max-width: 420px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.4);
}
.confirm-msg { color: var(--debug-text); font-size: 14px; margin: 0 0 16px; }
.confirm-actions { display: flex; gap: 10px; justify-content: flex-end; }
.confirm-btn {
  padding: 6px 18px; border-radius: 6px; border: 1px solid var(--debug-border);
  background: transparent; color: var(--debug-text); cursor: pointer; font-size: 13px;
}
.confirm-btn.cancel:hover { border-color: var(--debug-muted); }
.confirm-btn.danger { background: var(--debug-accent); color: #fff; border-color: var(--debug-accent); }
.confirm-btn.danger:hover { opacity: 0.85; }
</style>
```

- [ ] **Step 2: Create `frontend/src/debug/components/shared/ExpandableText.vue`**

```vue
<script setup lang="ts">
import { ref, computed } from 'vue'

const props = withDefaults(defineProps<{ text: string; maxLength?: number }>(), { maxLength: 120 })
const expanded = ref(false)

const display = computed(() => {
  if (!props.text) return ''
  if (expanded.value || props.text.length <= props.maxLength) return props.text
  return props.text.slice(0, props.maxLength) + '...'
})
const canExpand = computed(() => props.text && props.text.length > props.maxLength)
</script>

<template>
  <span>
    <span>{{ display }}</span>
    <button v-if="canExpand" class="expand-btn" @click="expanded = !expanded">
      {{ expanded ? '收起' : '展开' }}
    </button>
  </span>
</template>

<style scoped>
.expand-btn {
  background: none; border: 1px solid var(--debug-border); border-radius: 3px;
  color: var(--debug-accent); cursor: pointer; font-size: 11px; padding: 0 4px;
  margin-left: 4px; vertical-align: middle;
}
</style>
```

- [ ] **Step 3: Create `frontend/src/debug/components/shared/PaginationBar.vue`**

```vue
<script setup lang="ts">
import { ref, computed } from 'vue'

const props = defineProps<{ total: number; perPage: number; currentOffset: number }>()
const emit = defineEmits<{ 'update:perPage': [value: number]; goToOffset: [offset: number] }>()

const totalPages = computed(() => Math.max(1, Math.ceil(props.total / props.perPage)))
const currentPage = computed(() => Math.floor(props.currentOffset / props.perPage) + 1)

const jumpInput = ref('')

function goToPage(page: number) {
  const p = Math.max(1, Math.min(page, totalPages.value))
  emit('goToOffset', (p - 1) * props.perPage)
}

function onJump() {
  const n = parseInt(jumpInput.value)
  if (n >= 1) { goToPage(n); jumpInput.value = '' }
}

function onPerPageChange(e: Event) {
  emit('update:perPage', Number((e.target as HTMLSelectElement).value))
}
</script>

<template>
  <div class="pagination">
    <button :disabled="currentPage <= 1" @click="goToPage(currentPage - 1)">←</button>
    <span>第 {{ currentPage }}/{{ totalPages }} 页</span>
    <button :disabled="currentPage >= totalPages" @click="goToPage(currentPage + 1)">→</button>
    <select :value="perPage" @change="onPerPageChange" class="per-page-select">
      <option :value="50">50</option>
      <option :value="100">100</option>
      <option :value="200">200</option>
    </select>
    <span class="jump-label">前往</span>
    <input v-model="jumpInput" class="jump-input" size="4" @keydown.enter="onJump" placeholder="#" />
    <button @click="onJump">跳转</button>
  </div>
</template>

<style scoped>
.pagination { display: flex; align-items: center; gap: 6px; font-size: 12px; padding: 8px 0; color: var(--debug-muted); }
.pagination button {
  padding: 2px 8px; border: 1px solid var(--debug-border); border-radius: 4px;
  background: transparent; color: var(--debug-text); cursor: pointer; font-size: 12px;
}
.pagination button:disabled { opacity: 0.4; cursor: default; }
.pagination button:hover:not(:disabled) { border-color: var(--debug-accent); }
.per-page-select { background: var(--debug-bg); color: var(--debug-text); border: 1px solid var(--debug-border); border-radius: 4px; padding: 2px 4px; font-size: 12px; }
.jump-input { background: var(--debug-bg); color: var(--debug-text); border: 1px solid var(--debug-border); border-radius: 4px; padding: 2px 6px; font-size: 12px; width: 48px; }
.jump-label { font-size: 12px; }
</style>
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/debug/components/shared/
git commit -m "feat(debug): add PaginationBar, ConfirmModal, ExpandableText shared components"
```

---

### Task 5: OverviewTab.vue (panels 1-8)

**Files:**
- Create: `frontend/src/debug/components/OverviewTab.vue`

**Interfaces:**
- Consumes: `useDebugStore` from `@/stores/debugStore`, types from `@/debug/types`
- Produces: First 8 of 16 panels in a 2-column CSS grid

- [ ] **Step 1: Create `frontend/src/debug/components/OverviewTab.vue`**

Panel helper functions inline. Full component (~250 lines):

```vue
<script setup lang="ts">
import { ref, computed } from 'vue'
import { useDebugStore } from '@/stores/debugStore'
import ExpandableText from './shared/ExpandableText.vue'
import ImageLightbox from './modals/ImageLightbox.vue'
import PromptViewer from './modals/PromptViewer.vue'
import ResponseViewer from './modals/ResponseViewer.vue'
import VersionHistory from './modals/VersionHistory.vue'
import type { ConsolidationInfo, MemoryReflectorInfo, CharacterReflectorInfo, CharacterEngineInfo, SessionRecentItem, ContentPart } from '@/debug/types'

const store = useDebugStore()
const d = computed(() => store.data)

// Shared refs for modal control
const promptIndex = ref(-1)
const responseIndex = ref(-1)
const versionType = ref<'l2' | 'c6'>('l2')
const showVersions = ref(false)
const lightboxSrc = ref('')

function openLightbox(src: string) { lightboxSrc.value = src }

function openVersions(type: 'l2' | 'c6') { versionType.value = type; showVersions.value = true }

// Memory type color
function memoryColor(t: string) {
  const m: Record<string, string> = { fact: '#4caf50', plan: '#2196f3', preference: '#9c27b0', event: '#ff9800' }
  return m[t.toLowerCase()] || '#888'
}

// Extract images from ContentPart[]
function extractImages(parts: ContentPart[]): ContentPart[] {
  return parts.filter(p => p.type === 'image' && p.src)
}

// Grouped props for panels 13-16
const consolidationProps = computed<ConsolidationInfo | null>(() => {
  if (!d.value) return null
  return { buffer_size: d.value.consolidator_buffer_size, threshold: d.value.consolidator_threshold, history: d.value.consolidation_history }
})
const memReflectorProps = computed<MemoryReflectorInfo | null>(() => {
  if (!d.value) return null
  const v = d.value
  return { exists: v.mreflector_exists, round_count: v.mreflector_round_count, round_threshold: v.mreflector_round_threshold, new_memory_count: v.mreflector_new_memory_count, memory_threshold: v.mreflector_memory_threshold, locked: v.mreflector_locked, last_summary: v.mreflector_last_summary }
})
const charReflectorProps = computed<CharacterReflectorInfo | null>(() => {
  if (!d.value) return null
  const v = d.value
  return { exists: v.reflector_exists, buffer_size: v.reflector_buffer_size, last_reflection_at: v.reflector_last_reflection_at ?? undefined, locked: v.reflector_locked, self_perception_history: v.self_perception_history, last_prompt_system: v.reflector_last_prompt_system, last_prompt_user: v.reflector_last_prompt_user, last_text_len: v.reflector_last_text_len, last_duration_ms: v.reflector_last_duration_ms, last_success: v.reflector_last_success }
})
const charEngineProps = computed<CharacterEngineInfo | null>(() => {
  if (!d.value) return null
  const v = d.value
  return { exists: v.character_engine_exists, self_l2: v.self_l2, state_l3: v.state_l3, mood: v.mood, last_awareness_time: v.last_awareness_time, l2_version_count: v.l2_version_count, c6_version_count: v.c6_version_count, state_history: v.state_history }
})

function fsmColor(s: string) { return s === 'idle' ? 'var(--debug-green)' : 'var(--debug-warn)' }

// Token model name shortener
function shortModel(name: string) { return name.split('/').pop() || name }
</script>
```

(continued in template and style below)

- [ ] **Step 2: Add template section for panels 1-8**

```vue
<template>
  <div v-if="!d" class="status">加载中...</div>
  <div v-else class="grid">
    <!-- 1. System -->
    <div class="card">
      <h2>System</h2>
      <div class="row"><span class="label">FSM</span><span :style="{ color: fsmColor(d.fsm_state), fontWeight: 600 }">{{ d.fsm_state }}</span></div>
      <div class="row" v-if="d.fsm_previous"><span class="label">Previous</span><span>{{ d.fsm_previous }}</span></div>
      <div class="row"><span class="label">Uptime</span><span>{{ Math.round(d.uptime_seconds) }}s</span></div>
      <div class="row"><span class="label">LLM</span><span>{{ d.llm_model }}</span></div>
      <div class="row"><span class="label">TTS</span><span :class="d.tts_ok ? 'ok' : 'fail'">{{ d.tts_ok ? '✓' : '✗' }}</span></div>
      <div class="row"><span class="label">STT</span><span :class="d.stt_ok ? 'ok' : 'fail'">{{ d.stt_ok ? '✓' : '✗' }}</span></div>
      <div class="row"><span class="label">WS Clients</span><span>{{ d.ws_clients }}</span></div>
      <div class="row"><span class="label">Proactive</span><span>{{ d.proactive_enabled ? 'ON' : 'OFF' }}</span></div>
      <div class="row" v-if="d.last_interaction_ago != null"><span class="label">Last Interaction</span><span>{{ Math.round(d.last_interaction_ago) }}s ago</span></div>
    </div>

    <!-- 2. Memory -->
    <div class="card">
      <h2>Memory</h2>
      <div class="row"><span class="label">Session</span><span>{{ d.session_messages }} msgs</span></div>
      <div class="row"><span class="label">LTM</span><span>{{ d.lt_count }}</span></div>
      <div class="row"><span class="label">MongoDB</span><span :class="d.mongodb_ok ? 'ok' : 'fail'">{{ d.mongodb_ok ? '✓' : '✗' }}</span></div>
      <div class="row"><span class="label">Milvus</span><span :class="d.milvus_ok ? 'ok' : 'fail'">{{ d.milvus_ok ? '✓' : '✗' }}</span></div>
      <div class="row"><span class="label">Embedding</span><span :class="d.embedding_ok ? 'ok' : 'fail'">{{ d.embedding_ok ? '✓' : '✗' }}</span></div>
      <div class="row"><span class="label">Archive</span><span>{{ d.archive_msg_count }}</span></div>
      <div class="row"><span class="label">Character</span><span>{{ d.character_name }}</span></div>
      <ExpandableText :text="d.base_personality_summary" />
      <details v-if="d.character_state_raw" class="raw-details">
        <summary>Character State Raw</summary>
        <pre>{{ JSON.stringify(d.character_state_raw, null, 2) }}</pre>
      </details>
    </div>

    <!-- 3. Latency (hidden if no data) -->
    <div v-if="d.timing_stats?.count" class="card">
      <h2>Latency</h2>
      <div class="row"><span class="label">Count</span><span>{{ d.timing_stats.count }}</span></div>
      <div class="row"><span class="label">Avg</span><span>{{ d.timing_stats.avg_ms }}ms</span></div>
      <div class="row"><span class="label">P50</span><span>{{ d.timing_stats.p50_ms }}ms</span></div>
      <div class="row"><span class="label">P95</span><span>{{ d.timing_stats.p95_ms }}ms</span></div>
      <div class="row"><span class="label">P99</span><span>{{ d.timing_stats.p99_ms }}ms</span></div>
      <div class="row"><span class="label">Last</span><span>{{ d.timing_stats.last_ms }}ms</span></div>
    </div>

    <!-- 4. Process Memory -->
    <div class="card">
      <h2>Process Memory</h2>
      <div class="row"><span class="label">RSS</span><span>{{ d.memory?.rss_mb }} MB</span></div>
      <div class="row"><span class="label">VMS</span><span>{{ d.memory?.vms_mb }} MB</span></div>
      <div class="row"><span class="label">PCT</span><span>{{ d.memory?.pct }}%</span></div>
      <div class="row"><span class="label">Threads</span><span>{{ d.memory?.num_threads }}</span></div>
    </div>

    <!-- 5. Scheduler -->
    <div class="card">
      <h2>Scheduler</h2>
      <div class="row"><span class="label">Pending</span><span>{{ d.scheduler_pending }}</span></div>
      <div class="row"><span class="label">Running</span><span>{{ d.scheduler_running }}</span></div>
      <div class="row"><span class="label">Total</span><span>{{ d.scheduler_total }}</span></div>
      <ExpandableText :text="JSON.stringify(d.task_tree, null, 2)" :maxLength="120" />
    </div>

    <!-- 6. Token -->
    <div class="card">
      <h2>Token Consumption</h2>
      <div class="row"><span class="label">Session Total</span><span>{{ d.token_stats.session_total }}</span></div>
      <div v-for="(tokens, model) in d.token_stats.per_model" :key="model" class="row">
        <span class="label">{{ shortModel(model) }}</span><span>{{ tokens }}</span>
      </div>
      <div v-if="d.pending_token_count" class="warn">⚠ {{ d.pending_token_count }} pending token snapshots</div>
      <div v-if="d.pending_session_exists" class="warn">⚠ Pending session data exists</div>
    </div>

    <!-- 7. LLM Prompt -->
    <div class="card">
      <h2>LLM Prompt</h2>
      <div v-if="!d.prompt_summary?.records?.length" class="status">暂无</div>
      <div v-for="r in d.prompt_summary.records" :key="r.index" class="record-row">
        <span>#{{ r.index }} R{{ r.round_num }}</span>
        <span>{{ r.message_count }} msgs · {{ r.total_chars }} chars</span>
        <span class="muted">{{ r.model }}</span>
        <button class="btn-sm" @click="promptIndex = r.index">View</button>
      </div>
    </div>

    <!-- 8. LLM Response -->
    <div class="card">
      <h2>LLM Response</h2>
      <div v-if="!d.response_summary?.records?.length" class="status">暂无</div>
      <div v-for="r in d.response_summary.records" :key="r.index" class="record-row">
        <span>#{{ r.index }} R{{ r.round_num }}</span>
        <span>{{ r.text_len }} chars · IV: {{ r.inner_voice_len }}</span>
        <button class="btn-sm" @click="responseIndex = r.index">View</button>
      </div>
    </div>
  </div>
</template>
```

**Note:** Panels 9-16 follow the same `.card` + `.row` pattern. See Task 6.

- [ ] **Step 3: Add style section**

```vue
<style scoped>
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.card { background: var(--debug-surface); border: 1px solid var(--debug-border); border-radius: 8px; padding: 12px 14px; overflow: hidden; }
.card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 1px; color: var(--debug-accent); margin: 0 0 8px; border-bottom: 1px solid var(--debug-border); padding-bottom: 4px; }
.row { display: flex; justify-content: space-between; padding: 2px 0; font-size: 12px; }
.label { color: var(--debug-muted); }
.ok { color: var(--debug-green); }
.fail { color: var(--debug-red); }
.warn { color: var(--debug-warn); font-size: 12px; padding: 4px 0; }
.status { color: var(--debug-muted); font-size: 12px; padding: 4px 0; }
.record-row { display: flex; gap: 8px; align-items: center; padding: 3px 0; font-size: 12px; border-bottom: 1px solid rgba(255,255,255,0.04); }
.record-row .muted { color: var(--debug-muted); font-size: 11px; }
.record-row .btn-sm { margin-left: auto; padding: 1px 8px; border: 1px solid var(--debug-border); border-radius: 3px; background: transparent; color: var(--debug-accent); cursor: pointer; font-size: 11px; }
.raw-details { margin-top: 4px; }
.raw-details summary { cursor: pointer; font-size: 11px; color: var(--debug-muted); }
.raw-details pre { font-size: 10px; white-space: pre-wrap; word-break: break-all; max-height: 150px; overflow-y: auto; margin-top: 4px; }
.ltm-tag { font-size: 10px; padding: 1px 6px; border-radius: 3px; color: #fff; font-weight: 600; }
</style>
```

- [ ] **Step 4: Build to verify**

```bash
cd frontend && npx vue-tsc --noEmit 2>&1 | grep -c "error" && npx vite build 2>&1 | tail -3
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/debug/components/OverviewTab.vue
git commit -m "feat(debug): add OverviewTab panels 1-8 (System through Response)"
```

---

### Task 6: OverviewTab.vue (panels 9-16)

**Files:**
- Modify: `frontend/src/debug/components/OverviewTab.vue` (add panels 9-16 template)

**Interfaces:**
- Consumes: same as Task 5 — all store and type imports already defined
- Produces: Complete 16-panel overview with all computed props for panels 13-16

- [ ] **Step 1: Append panels 9-16 template to OverviewTab.vue**

All panels follow the `.card > h2 + .row` pattern established in Task 5. Key examples:

**Panel #9 — Recent Session:**
```vue
<!-- 9. Recent Session -->
<div class="card">
  <h2>Recent Session</h2>
  <div v-if="!d.session_recent?.length" class="status">暂无</div>
  <div v-for="(m, i) in d.session_recent" :key="i" class="session-msg">
    <span class="role-tag" :style="{ background: m.role === 'user' ? '#6af' : m.role === 'assistant' ? '#f6a' : '#888' }">{{ m.role }}</span>
    <ExpandableText :text="(m.parts || []).map(p => p.content).join(' ')" :maxLength="100" />
    <template v-for="p in (m.content || [])" :key="p.src">
      <img v-if="p.type === 'image' && p.src" :src="p.src" class="msg-thumb" @click="openLightbox(p.src)" />
    </template>
  </div>
</div>
```

**Panel #13 — Consolidation (using computed prop):**
```vue
<!-- 13. Consolidation -->
<div v-if="consolidationProps" class="card">
  <h2>Consolidation</h2>
  <div class="row"><span class="label">Buffer</span><span>{{ consolidationProps.buffer_size }} / {{ consolidationProps.threshold }}</span></div>
  <div v-if="consolidationProps.buffer_size >= consolidationProps.threshold" class="warn">⚠ Triggering</div>
  <div v-for="(h, i) in consolidationProps.history" :key="i" class="list-item">
    <span class="ltm-tag" :style="{ background: memoryColor(h.type) }">{{ h.type }}</span>
    <span>{{ h.content?.slice(0, 80) }}</span>
  </div>
</div>
```

Panels #10 (LTM Recent), #11 (Tools), #12 (Tool History), #14 (Memory Reflector), #15 (Character Reflector), #16 (CharacterEngine) follow these same patterns. For #15 and #16, open the VersionHistory modal via `@click="openVersions('l2')"` and `@click="openVersions('c6')"` respectively. Panel #16 should also show C9 cooldown: `Math.max(0, Math.round((Date.now()/1000 - d.last_awareness_time) / 60))` minutes remaining if under 60 min.

Add these CSS additions for session messages and role tags used in panels 9-16:
```css
.session-msg { padding: 3px 0; border-bottom: 1px solid rgba(255,255,255,0.04); font-size: 12px; display: flex; gap: 6px; align-items: baseline; }
.role-tag { font-size: 9px; padding: 1px 5px; border-radius: 3px; color: #fff; font-weight: 600; white-space: nowrap; flex-shrink: 0; }
.msg-thumb { max-width: 60px; max-height: 60px; border-radius: 4px; cursor: pointer; border: 1px solid var(--debug-border); margin: 2px 0; }
.list-item { padding: 3px 0; border-bottom: 1px solid rgba(255,255,255,0.04); font-size: 12px; display: flex; gap: 6px; align-items: baseline; }
```

- [ ] **Step 2: Build to verify**

```bash
cd frontend && npx vite build 2>&1 | tail -3
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/debug/components/OverviewTab.vue
git commit -m "feat(debug): add OverviewTab panels 9-16 (Recent Session through CharacterEngine)"
```

---

### Task 7: Modal viewers — PromptViewer + ResponseViewer

**Files:**
- Create: `frontend/src/debug/components/modals/PromptViewer.vue`
- Create: `frontend/src/debug/components/modals/ResponseViewer.vue`

**Interfaces:**
- Produces:
  - `PromptViewer` — Props: `index: number`, `visible: boolean`; Emits: `close`, `nav(delta)`
  - `ResponseViewer` — Props: `index: number`, `visible: boolean`; Emits: `close`, `nav(delta)`
- Consumes: `PromptRecord`, `ResponseRecord` from `@/debug/types`

- [ ] **Step 1: Create `frontend/src/debug/components/modals/ResponseViewer.vue`**

```vue
<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{ index: number; visible: boolean }>()
const emit = defineEmits<{ close: []; nav: [delta: number] }>()

interface ResponseDetail { index: number; timestamp: string; round_num: number; raw_text: string; inner_voice: string; text_len: number; inner_voice_len: number }
const detail = ref<ResponseDetail | null>(null)
const loading = ref(false)
const error = ref('')

async function fetchResponse() {
  if (!props.visible) return
  loading.value = true; error.value = ''
  try {
    const res = await fetch(`/debug/api/responses/${props.index}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    detail.value = await res.json()
  } catch (e) { error.value = (e as Error).message; detail.value = null }
  finally { loading.value = false }
}

watch(() => [props.index, props.visible], () => { if (props.visible) fetchResponse() })

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'ArrowLeft') emit('nav', -1)
  else if (e.key === 'ArrowRight') emit('nav', 1)
  else if (e.key === 'Escape') emit('close')
}
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="modal-overlay" @click.self="emit('close')" @keydown="onKeydown" tabindex="0">
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
```

- [ ] **Step 2: Create `frontend/src/debug/components/modals/PromptViewer.vue`**

```vue
<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{ index: number; visible: boolean }>()
const emit = defineEmits<{ close: []; nav: [delta: number] }>()

interface PromptDetail { index: number; timestamp: string; round_num: number; model: string; messages: { role: string; content: unknown; [key: string]: unknown }[]; tools: unknown[]; template_source?: { path: string; sections: Record<string, string> } }
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

watch(() => [props.index, props.visible], () => { if (props.visible) fetchPrompt() })

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
    <div v-if="visible" class="modal-overlay" @click.self="emit('close')" @keydown="onKeydown" tabindex="0">
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
```

- [ ] **Step 3: Build to verify**

```bash
cd frontend && npx vite build 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/debug/components/modals/PromptViewer.vue frontend/src/debug/components/modals/ResponseViewer.vue
git commit -m "feat(debug): add PromptViewer and ResponseViewer modal components"
```

---

### Task 8: Database Tab — LtmPanel

**Files:**
- Create: `frontend/src/debug/components/DatabaseTab.vue`
- Create: `frontend/src/debug/components/LtmPanel.vue`

**Interfaces:**
- Produces:
  - `DatabaseTab` — container with three child panels
  - `LtmPanel` — CRUD panel with search, filter, pagination, inline edit, batch delete, create modal
- Consumes: `PaginationBar`, `ConfirmModal`, `ExpandableText` from `./shared/`

- [ ] **Step 1: Create `frontend/src/debug/components/DatabaseTab.vue`**

```vue
<script setup lang="ts">
import LtmPanel from './LtmPanel.vue'
import SessionPanel from './SessionPanel.vue'
import ArchivePanel from './ArchivePanel.vue'
</script>

<template>
  <section class="db-tab">
    <LtmPanel />
    <SessionPanel />
    <ArchivePanel />
  </section>
</template>

<style scoped>
.db-tab { display: flex; flex-direction: column; gap: 16px; }
</style>
```

- [ ] **Step 2: Create `frontend/src/debug/components/LtmPanel.vue`**

Full CRUD panel with search, type filter, paginated list, inline edit, batch delete, create modal. Insert panel between three CRUD section elements.

```vue
<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useDebugStore } from '@/stores/debugStore'
import PaginationBar from './shared/PaginationBar.vue'
import ConfirmModal from './shared/ConfirmModal.vue'
import ExpandableText from './shared/ExpandableText.vue'

interface LtmEntry { id: string; text: string; memory_type: string; poignancy: number; timestamp: string; metadata?: Record<string, unknown> }

const store = useDebugStore()

const entries = ref<LtmEntry[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const search = ref('')
const typeFilter = ref('')
const selectedIds = ref<Set<string>>(new Set())
const editingId = ref<string | null>(null)
const editText = ref('')

// Confirm state — 使用 local ref 避免与 SessionPanel/ArchivePanel 的 ConfirmModal 冲突
const confirmVisible = ref(false)
const confirmMsg = ref('')
const pendingConfirm = ref<(() => Promise<void>) | null>(null)

// Create modal
const createVisible = ref(false)
const createText = ref('')
const createType = ref('note')
const createPoignancy = ref(5)

const perPage = ref(50)

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
watch([search, typeFilter], () => { store.ltmPage.offset = 0; load() })

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
  await fetch(`/debug/api/ltm/${id}`, { method: 'DELETE' })
  load()
}

async function deleteSelected() {
  for (const id of selectedIds.value) { await fetch(`/debug/api/ltm/${id}`, { method: 'DELETE' }) }
  selectedIds.value = new Set()
  load()
}

async function clearAll() {
  await fetch('/debug/api/ltm', { method: 'DELETE' })
  load()
}

function startEdit(entry: LtmEntry) { editingId.value = entry.id; editText.value = entry.text }

async function saveEdit(id: string) {
  await fetch(`/debug/api/ltm/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: editText.value }) })
  editingId.value = null
  load()
}

async function createEntry() {
  await fetch('/debug/api/ltm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: createText.value, memory_type: createType.value, poignancy: createPoignancy.value }) })
  createVisible.value = false; createText.value = ''; load()
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
      <select v-model="typeFilter" class="field" @change="load">
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
        <span class="row-meta">{{ e.poignancy }} · {{ e.timestamp?.slice(0, 16) || '' }}</span>
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
.list-row:last-child { border-bottom: none; }
.type-tag { font-size: 10px; padding: 1px 6px; border-radius: 3px; color: #fff; white-space: nowrap; font-weight: 600; min-width: 60px; text-align: center; }
.row-text { flex: 1; min-width: 0; }
.row-meta { font-size: 10px; color: var(--debug-muted); white-space: nowrap; }
.field-label { display: block; color: var(--debug-muted); font-size: 11px; margin: 8px 0 3px; }
.modal-overlay { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center; }
.modal-box { background: var(--debug-surface); border: 1px solid var(--debug-border); border-radius: 8px; padding: 20px; min-width: 360px; max-width: 500px; box-shadow: 0 4px 20px rgba(0,0,0,0.4); }
.modal-box h3 { margin: 0 0 12px; font-size: 15px; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
</style>
```

- [ ] **Step 2: Build to verify (expected: SystemPanel/ArchivePanel import failures — will resolve in Task 9)**

```bash
cd frontend && npx vite build 2>&1 | tail -10
# Expected: "Could not resolve './SessionPanel.vue'" and "'./ArchivePanel.vue'" — these are created in Task 9
# LtmPanel.vue alone builds correctly; the error is from DatabaseTab.vue's import of not-yet-created siblings
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/debug/components/DatabaseTab.vue frontend/src/debug/components/LtmPanel.vue
git commit -m "feat(debug): add DatabaseTab with LtmPanel CRUD"
```

---

### Task 9: Database Tab — SessionPanel + ArchivePanel

**Files:**
- Create: `frontend/src/debug/components/SessionPanel.vue`
- Create: `frontend/src/debug/components/ArchivePanel.vue`

- [ ] **Step 1: Create `frontend/src/debug/components/SessionPanel.vue`**

```vue
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
        <button v-if="selectedIds.size" class="btn danger" @click="confirmThen(() => deleteSessions([...selectedIds.value]), `删除选中的 ${selectedIds.size} 个 session？`)">删除选中 ({{ selectedIds.size }})</button>
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
```

- [ ] **Step 2: Create `frontend/src/debug/components/ArchivePanel.vue`**

```vue
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
watch([search, roleFilter], () => { store.archivePage.offset = 0; load() })

function onPageChange(newOffset: number) { store.archivePage.offset = newOffset; load() }

function toggleSelect(id: string) { const s = new Set(selectedIds.value); s.has(id) ? s.delete(id) : s.add(id); selectedIds.value = s }
function toggleAll() { if (selectedIds.value.size === entries.value.length) selectedIds.value = new Set(); else selectedIds.value = new Set(entries.value.map(e => e._id)) }

async function deleteEntry(id: string) { await fetch(`/debug/api/archive/${id}`, { method: 'DELETE' }); load() }
async function deleteSelected() { for (const id of selectedIds.value) await fetch(`/debug/api/archive/${id}`, { method: 'DELETE' }); selectedIds.value = new Set(); load() }
async function clearAll() { await fetch('/debug/api/archive', { method: 'DELETE' }); load() }

function startEdit(e: ArchiveEntry) { editingId.value = e._id; editText.value = (e.parts || []).map(p => p.content).join(' ') }
async function saveEdit(id: string) { await fetch(`/debug/api/archive/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: editText.value }) }); editingId.value = null; load() }

function roleColor(r: string) { const m: Record<string, string> = { user: '#6af', assistant: '#f6a', tool: '#fa6' }; return m[r] || '#888' }
function confirmThen(fn: () => Promise<void>, msg: string) { confirmMsg.value = msg; confirmVisible.value = true; pendingConfirm.value = fn }
async function onConfirm() { confirmVisible.value = false; await pendingConfirm.value?.() }
</script>
```
(with template and style sections matching LtmPanel pattern but with role filter and role-colored tags)

- [ ] **Step 3: Build to verify**

```bash
cd frontend && npx vite build 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/debug/components/SessionPanel.vue frontend/src/debug/components/ArchivePanel.vue
git commit -m "feat(debug): add SessionPanel and ArchivePanel for Database tab"
```

---

### Task 10: Tasks Tab — SSE + vis-network DAG

**Files:**
- Create: `frontend/src/debug/components/TasksTab.vue`
- Create: `frontend/src/debug/components/TaskCard.vue`

**Interfaces:**
- Produces:
  - `TasksTab` — SSE EventSource lifecycle, connection indicator, active/done task lists
  - `TaskCard` — Props: `task` object with nodes/edges; renders vis-network DAG

- [ ] **Step 1: Create `frontend/src/debug/components/TaskCard.vue`**

```vue
<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { Network } from 'vis-network'
import { DataSet } from 'vis-data'

interface TaskNode { node_id: string; status: string; attempt: number; summary: string; error?: string }
interface TaskEdge { 0: string; 1: string } // [from, to]

const props = defineProps<{
  task: { task_id: string; status: string; plan_version: number; created_at: string; mailbox_depth: number; nodes: TaskNode[]; edges: TaskEdge[] }
}>()

const d3Container = ref<HTMLDivElement | null>(null)
let network: Network | null = null

const STATUS_COLORS: Record<string, string> = { pending: '#ffa726', running: '#42a5f5', success: '#66bb6a', failed: '#ef5350', skipped: '#bdbdbd' }
const TASK_STATUS_COLORS: Record<string, string> = { running: '#42a5f5', completed: '#66bb6a', failed: '#ef5350', cancelled: '#bdbdbd' }

function taskColor(s: string) { return TASK_STATUS_COLORS[s] || '#888' }

function renderDag() {
  if (!d3Container.value || !props.task.nodes.length) return
  if (network) { network.destroy(); network = null }

  const nodes = new DataSet(props.task.nodes.map(n => ({
    id: n.node_id,
    label: `${n.node_id}\n${n.summary?.slice(0, 20) || ''}`,
    color: { background: STATUS_COLORS[n.status] || '#888', border: '#333' },
    font: { size: 11, color: '#ccc' },
    shape: 'box',
    margin: { top: 8, bottom: 8, left: 10, right: 10 },
  })))

  const edges = new DataSet(props.task.edges.map((e, i) => ({
    id: i, from: e[0], to: e[1], arrows: 'to',
    color: { color: '#555' },
  })))

  network = new Network(d3Container.value, { nodes, edges }, {
    layout: { hierarchical: { direction: 'TB', sortMethod: 'directed', nodeSpacing: 80, levelSeparation: 100 } },
    physics: false,
    interaction: { zoomView: true, dragView: true },
    edges: { smooth: { type: 'cubicBezier' } },
    height: '300px',
  })
}

onMounted(() => nextTick(renderDag))
onUnmounted(() => { if (network) { network.destroy(); network = null } })

const successCount = computed(() => props.task.nodes.filter(n => n.status === 'success').length)
const skippedCount = computed(() => props.task.nodes.filter(n => n.status === 'skipped').length)
const totalCount = computed(() => props.task.nodes.length)
const progressPct = computed(() => totalCount.value ? Math.round((successCount.value + skippedCount.value) / totalCount.value * 100) : 0)
</script>

<template>
  <div class="task-card">
    <div class="task-head">
      <span class="task-id">{{ task.task_id?.slice(0, 20) }}...</span>
      <span class="task-status" :style="{ background: taskColor(task.status) }">{{ task.status }}</span>
      <span class="task-meta">v{{ task.plan_version }} · {{ task.nodes.length }} nodes · mailbox: {{ task.mailbox_depth }}</span>
    </div>
    <div class="progress-bar">
      <div class="progress-fill" :style="{ width: progressPct + '%' }" />
      <span class="progress-text">{{ successCount }}+{{ skippedCount }}/{{ totalCount }} ({{ progressPct }}%)</span>
    </div>
    <div ref="d3Container" class="dag-container" />
  </div>
</template>

<style scoped>
.task-card { background: var(--debug-surface); border: 1px solid var(--debug-border); border-radius: 8px; padding: 12px; margin-bottom: 10px; }
.task-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
.task-id { font-family: monospace; font-size: 12px; color: var(--debug-text); }
.task-status { font-size: 10px; padding: 2px 8px; border-radius: 3px; color: #fff; font-weight: 600; text-transform: uppercase; }
.task-meta { font-size: 11px; color: var(--debug-muted); }
.progress-bar { height: 16px; background: var(--debug-bg); border-radius: 8px; position: relative; margin-bottom: 8px; overflow: hidden; }
.progress-fill { height: 100%; background: var(--debug-green); border-radius: 8px; transition: width 0.3s; }
.progress-text { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 10px; color: #fff; text-shadow: 0 0 4px rgba(0,0,0,0.6); }
.dag-container { border: 1px solid var(--debug-border); border-radius: 4px; height: 300px; }
</style>
```

- [ ] **Step 2: Create `frontend/src/debug/components/TasksTab.vue`**

```vue
<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import TaskCard from './TaskCard.vue'

interface TaskNode { node_id: string; status: string; attempt: number; summary: string; error?: string }
interface Task { task_id: string; status: string; plan_version: number; created_at: string; error?: string; error_code?: string; phase: string; mailbox_depth: number; nodes: TaskNode[]; edges: [string, string][] }

const tasks = ref<Task[]>([])
const doneTasks = ref<Task[]>([])
const connState = ref<'connected' | 'reconnecting' | 'disconnected'>('disconnected')
const connColor = { connected: 'var(--debug-green)', reconnecting: 'var(--debug-warn)', disconnected: 'var(--debug-red)' }

let es: EventSource | null = null
let reconnectDelay = 1000
let reconnectTimer: ReturnType<typeof setTimeout> | null = null

function connect() {
  if (es) { es.close(); es = null }
  es = new EventSource('/debug/api/tasks/stream')
  connState.value = 'connected'
  reconnectDelay = 1000

  es.addEventListener('snapshot', (e: MessageEvent) => {
    const data = JSON.parse(e.data)
    tasks.value = Array.isArray(data) ? data : []
  })
  es.addEventListener('task_new', (e: MessageEvent) => {
    const t = JSON.parse(e.data)
    if (!tasks.value.find(x => x.task_id === t.task_id)) tasks.value.unshift(t)
  })
  es.addEventListener('task_update', (e: MessageEvent) => {
    const t = JSON.parse(e.data)
    const idx = tasks.value.findIndex(x => x.task_id === t.task_id)
    if (idx >= 0) tasks.value[idx] = { ...tasks.value[idx], ...t }
  })
  es.addEventListener('task_done', (e: MessageEvent) => {
    const t = JSON.parse(e.data)
    tasks.value = tasks.value.filter(x => x.task_id !== t.task_id)
    if (!doneTasks.value.find(x => x.task_id === t.task_id)) doneTasks.value.unshift(t)
  })
  es.addEventListener('node_update', (e: MessageEvent) => {
    const { task_id, node_id, status, attempt } = JSON.parse(e.data)
    const task = tasks.value.find(x => x.task_id === task_id)
    if (task) { const node = task.nodes.find(n => n.node_id === node_id); if (node) { node.status = status; node.attempt = attempt } }
  })

  es.onerror = () => {
    es?.close(); es = null
    connState.value = 'reconnecting'
    reconnectTimer = setTimeout(() => { reconnectDelay = Math.min(reconnectDelay * 2, 30000); connect() }, reconnectDelay)
  }
}

onMounted(connect)
onUnmounted(() => {
  es?.close()
  if (reconnectTimer) clearTimeout(reconnectTimer)
})
</script>

<template>
  <div>
    <div class="conn-indicator">
      <span class="conn-dot" :style="{ background: connColor[connState] }" />
      {{ connState === 'connected' ? '已连接' : connState === 'reconnecting' ? '重连中...' : '已断开' }}
    </div>

    <h3 v-if="tasks.length">活跃任务 ({{ tasks.length }})</h3>
    <TaskCard v-for="t in tasks" :key="t.task_id" :task="t" />

    <details v-if="doneTasks.length" class="done-section">
      <summary>已完成 ({{ doneTasks.length }})</summary>
      <div v-for="t in doneTasks" :key="t.task_id" class="done-item">
        <span class="mono">{{ t.task_id?.slice(0, 20) }}...</span>
        <span class="task-status" :style="{ background: t.status === 'completed' ? 'var(--debug-green)' : t.status === 'failed' ? 'var(--debug-red)' : '#888' }">{{ t.status }}</span>
        <span v-if="t.error" class="error-text">{{ t.error }}</span>
      </div>
    </details>
  </div>
</template>

<style scoped>
.conn-indicator { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--debug-muted); margin-bottom: 12px; }
.conn-dot { width: 8px; height: 8px; border-radius: 50%; }
h3 { font-size: 14px; color: var(--debug-accent); margin: 12px 0 8px; }
.done-section { margin-top: 16px; }
.done-section summary { cursor: pointer; font-size: 13px; color: var(--debug-muted); margin-bottom: 8px; }
.done-item { display: flex; gap: 10px; align-items: center; padding: 4px 0; font-size: 12px; border-bottom: 1px solid rgba(255,255,255,0.04); }
.mono { font-family: monospace; font-size: 11px; }
.task-status { font-size: 10px; padding: 1px 6px; border-radius: 3px; color: #fff; font-weight: 600; }
.error-text { color: var(--debug-red); font-size: 11px; }
</style>
```

- [ ] **Step 3: Build to verify**

```bash
cd frontend && npx vite build 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/debug/components/TasksTab.vue frontend/src/debug/components/TaskCard.vue
git commit -m "feat(debug): add TasksTab with SSE streaming and vis-network DAG"
```

---

### Task 11: Remaining modals — VersionHistory + ImageLightbox

**Files:**
- Create: `frontend/src/debug/components/modals/VersionHistory.vue`
- Create: `frontend/src/debug/components/modals/ImageLightbox.vue`

- [ ] **Step 1: Create `frontend/src/debug/components/modals/ImageLightbox.vue`**

```vue
<script setup lang="ts">
const props = defineProps<{ src: string; visible: boolean }>()
const emit = defineEmits<{ close: [] }>()
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="lightbox-overlay" @click="emit('close')">
      <img :src="src" class="lightbox-img" @click.stop />
    </div>
  </Teleport>
</template>

<style scoped>
.lightbox-overlay { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,0.85); display: flex; align-items: center; justify-content: center; cursor: pointer; }
.lightbox-img { max-width: 90vw; max-height: 90vh; object-fit: contain; border-radius: 4px; cursor: default; }
</style>
```

- [ ] **Step 2: Create `frontend/src/debug/components/modals/VersionHistory.vue`**

```vue
<script setup lang="ts">
import { ref, watch } from 'vue'
import ConfirmModal from '../shared/ConfirmModal.vue'

const props = defineProps<{ contentType: 'l2' | 'c6'; visible: boolean }>()
const emit = defineEmits<{ close: [] }>()

interface VersionEntry { version: number; text: string; timestamp: string; trigger: string; change_summary?: string[] }
const versions = ref<VersionEntry[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const confirmVisible = ref(false)
const rollbackVersion = ref<number | null>(null)

async function load(before?: number) {
  loading.value = true; error.value = ''
  try {
    const params = new URLSearchParams({ type: props.contentType, limit: '20' })
    if (before) params.set('before', String(before))
    const res = await fetch(`/debug/api/character-versions?${params}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    versions.value = data.versions; total.value = data.total
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

watch(() => props.visible, (v) => { if (v) load() })

async function doRollback() {
  if (rollbackVersion.value === null) return
  await fetch('/debug/api/character-rollback', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: props.contentType, version: rollbackVersion.value }),
  })
  confirmVisible.value = false; rollbackVersion.value = null; load()
}

function promptRollback(v: number) { rollbackVersion.value = v; confirmVisible.value = true }
function label() { return props.contentType === 'l2' ? 'L2 Self' : 'C6 Speaking Style' }
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="modal-overlay" @click.self="emit('close')">
      <div class="modal-box wide">
        <div class="modal-header">
          <h3>{{ label() }} — 版本历史 ({{ total }})</h3>
          <button class="modal-close" @click="emit('close')">&times;</button>
        </div>
        <div v-if="loading" class="status">加载中...</div>
        <div v-else-if="error" class="status error">{{ error }}</div>
        <div v-else class="version-list">
          <div v-for="v in versions" :key="v.version" class="version-row">
            <span class="ver-num">v{{ v.version }}</span>
            <span class="ver-trigger">{{ v.trigger }}</span>
            <span class="ver-time">{{ v.timestamp?.slice(0, 16) || '' }}</span>
            <span class="ver-preview">{{ v.text?.slice(0, 80) }}...</span>
            <button class="btn-sm" @click="promptRollback(v.version)">回滚</button>
          </div>
        </div>
        <ConfirmModal :visible="confirmVisible" :message="`回滚到版本 ${rollbackVersion}？这将创建一条新的版本记录。`" @confirm="doRollback" @cancel="confirmVisible = false" />
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.modal-overlay { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; }
.modal-box { background: var(--debug-surface); border: 1px solid var(--debug-border); border-radius: 8px; padding: 20px; max-width: 700px; width: 90vw; max-height: 80vh; overflow-y: auto; box-shadow: 0 4px 24px rgba(0,0,0,0.5); }
.modal-box.wide { max-width: 700px; }
.modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.modal-header h3 { margin: 0; font-size: 15px; }
.modal-close { background: none; border: none; color: var(--debug-muted); font-size: 22px; cursor: pointer; }
.status { color: var(--debug-muted); padding: 12px; text-align: center; }
.status.error { color: var(--debug-red); }
.version-list { display: flex; flex-direction: column; gap: 4px; }
.version-row { display: flex; gap: 10px; align-items: center; padding: 6px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); font-size: 12px; }
.ver-num { font-weight: 600; color: var(--debug-accent); min-width: 36px; }
.ver-trigger { color: var(--debug-muted); font-size: 10px; min-width: 60px; }
.ver-time { color: var(--debug-muted); font-size: 10px; white-space: nowrap; }
.ver-preview { flex: 1; color: var(--debug-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.btn-sm { padding: 2px 8px; border: 1px solid var(--debug-border); border-radius: 3px; background: transparent; color: var(--debug-accent); cursor: pointer; font-size: 11px; }
.btn-sm:hover { border-color: var(--debug-accent); }
</style>
```

- [ ] **Step 3: Final build + verify**

```bash
cd frontend && npx vue-tsc --noEmit 2>&1 | tail -5
cd frontend && npx vite build 2>&1 | tail -10
```

- [ ] **Step 4: Clean old static assets not referenced by new build**

```bash
# Read current hash from debug.html
CURRENT_DEBUG_JS=$(grep -oP 'debug-\w+\.js' /home/yomu/Agent/static/debug.html)
CURRENT_DEBUG_CSS=$(grep -oP 'debug-\w+\.css' /home/yomu/Agent/static/debug.html)
# Remove any debug-*.js or debug-*.css in static/assets/ with different hashes
cd /home/yomu/Agent/static/assets
for f in debug-*.js; do [ "$f" != "$CURRENT_DEBUG_JS" ] && rm -v "$f"; done
for f in debug-*.css; do [ "$f" != "$CURRENT_DEBUG_CSS" ] && rm -v "$f"; done
```

- [ ] **Step 5: Final commit**

```bash
git add frontend/src/debug/components/modals/VersionHistory.vue frontend/src/debug/components/modals/ImageLightbox.vue
git add static/assets/ static/debug.html
git commit -m "feat(debug): add VersionHistory and ImageLightbox modals; final build"
```

---

## Verification Checklist

After all tasks complete:

1. `cd frontend && npm run build` — zero type errors, zero build errors
2. `ls static/assets/debug-*.js static/assets/debug-*.css` — only current-hash files remain (no stale duplicates)
3. Start server, visit `/debug`:
   - [ ] Three tabs render, Overview is default
   - [ ] 16 overview panels display data (no raw JSON dumps)
   - [ ] Pause button stops/resumes polling
   - [ ] Copy JSON buttons work
   - [ ] Database tab: LTM search/filter/paginate/CRUD functional
   - [ ] Database tab: Sessions list/detail/delete functional
   - [ ] Database tab: Archive search/filter/edit/delete functional
   - [ ] Tasks tab: SSE connects, DAG renders, done tasks collapse
   - [ ] Prompt/Response viewer modals open with ← → nav
   - [ ] Image lightbox opens on image click
   - [ ] VersionHistory modal lists versions with rollback
   - [ ] ConfirmModal appears for all destructive operations
