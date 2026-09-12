<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import TaskCard from './TaskCard.vue'
import { readSseStream } from '../sse'

interface TaskNode { node_id: string; status: string; attempt: number; summary: string; error?: string }
interface Task { task_id: string; status: string; plan_version: number; created_at: string; error?: string; error_code?: string; phase: string; mailbox_depth: number; nodes: TaskNode[]; edges: [string, string][] }

const tasks = ref<Task[]>([])
const doneTasks = ref<Task[]>([])
const connState = ref<'connected' | 'reconnecting' | 'disconnected'>('disconnected')
const connColor: Record<string, string> = { connected: 'var(--debug-green)', reconnecting: 'var(--debug-warn)', disconnected: 'var(--debug-red)' }

// 用 fetch+ReadableStream 而非 EventSource：后者无法带 Authorization 头，
// 而管理面凭据必须走头（见 ../sse.ts 与 ../auth.ts）。
let ctrl: AbortController | null = null
let reconnectDelay = 1000
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let disposed = false

function parse<T>(event: string, raw: string): T | null {
  try { return JSON.parse(raw) as T } catch (err) { console.warn(`SSE ${event} 解析失败:`, err); return null }
}

function onSnapshot(raw: string) {
  const data = parse<unknown>('snapshot', raw)
  if (data !== null) tasks.value = Array.isArray(data) ? (data as Task[]) : []
}

function onTaskNew(raw: string) {
  const t = parse<Task>('task_new', raw)
  if (t && !tasks.value.find(x => x.task_id === t.task_id)) tasks.value.unshift(t)
}

function onTaskUpdate(raw: string) {
  const t = parse<Task>('task_update', raw)
  if (!t) return
  const idx = tasks.value.findIndex(x => x.task_id === t.task_id)
  if (idx >= 0) tasks.value[idx] = { ...tasks.value[idx], ...t }
}

function onTaskDone(raw: string) {
  const t = parse<{ task_id: string; status: string; error?: string }>('task_done', raw)
  if (!t) return
  tasks.value = tasks.value.filter(x => x.task_id !== t.task_id)
  if (!doneTasks.value.find(x => x.task_id === t.task_id)) {
    doneTasks.value.unshift(t as unknown as Task)
    // 限制已完成任务列表最多 100 条，防止无限增长
    if (doneTasks.value.length > 100) doneTasks.value = doneTasks.value.slice(0, 100)
  }
}

function onNodeUpdate(raw: string) {
  const d = parse<{ task_id: string; node_id: string; status: string; attempt: number }>('node_update', raw)
  if (!d) return
  const task = tasks.value.find(x => x.task_id === d.task_id)
  const node = task?.nodes.find(n => n.node_id === d.node_id)
  if (node) { node.status = d.status; node.attempt = d.attempt }
}

const handlers: Record<string, (raw: string) => void> = {
  snapshot: onSnapshot,
  task_new: onTaskNew,
  task_update: onTaskUpdate,
  task_done: onTaskDone,
  node_update: onNodeUpdate,
  // heartbeat / 未知事件静默忽略；`: keepalive` 注释行由解析器丢弃，不会到这
}

function dispatch(event: string, raw: string) { handlers[event]?.(raw) }

async function connect() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  ctrl?.abort()
  ctrl = new AbortController()
  const myCtrl = ctrl
  try {
    // 注意：不在 connect() 内重置 reconnectDelay——
    // 重连会倍增，若在入口清零则指数退避失效，间隔恒 1000ms。
    // 在 onOpen（连接成功）时重置为初始值。
    await readSseStream('/debug/api/tasks/stream', dispatch, myCtrl.signal,
      () => { connState.value = 'connected'; reconnectDelay = 1000 })
  } catch (e) {
    if (myCtrl.signal.aborted || disposed) return   // 主动断开（卸载/重连），不排重连
    console.warn('SSE 连接中断:', (e as Error).message)
  }
  if (disposed || myCtrl.signal.aborted) return
  connState.value = 'reconnecting'
  reconnectTimer = setTimeout(() => {
    reconnectDelay = Math.min(reconnectDelay * 2, 30000)
    connect()
  }, reconnectDelay)
}

onMounted(connect)
onUnmounted(() => {
  disposed = true
  ctrl?.abort()
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
