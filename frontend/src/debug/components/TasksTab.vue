<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import TaskCard from './TaskCard.vue'

interface TaskNode { node_id: string; status: string; attempt: number; summary: string; error?: string }
interface Task { task_id: string; status: string; plan_version: number; created_at: string; error?: string; error_code?: string; phase: string; mailbox_depth: number; nodes: TaskNode[]; edges: [string, string][] }

const tasks = ref<Task[]>([])
const doneTasks = ref<Task[]>([])
const connState = ref<'connected' | 'reconnecting' | 'disconnected'>('disconnected')
const connColor: Record<string, string> = { connected: 'var(--debug-green)', reconnecting: 'var(--debug-warn)', disconnected: 'var(--debug-red)' }

let es: EventSource | null = null
let reconnectDelay = 1000
let reconnectTimer: ReturnType<typeof setTimeout> | null = null

function connect() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  if (es) { es.close(); es = null }
  es = new EventSource('/debug/api/tasks/stream')
  connState.value = 'connected'
  reconnectDelay = 1000

  es.addEventListener('snapshot', (e: MessageEvent) => {
    try {
      const data = JSON.parse(e.data)
      tasks.value = Array.isArray(data) ? data : []
    } catch (err) { console.warn('SSE snapshot 解析失败:', err) }
  })
  es.addEventListener('task_new', (e: MessageEvent) => {
    try {
      const t = JSON.parse(e.data)
      if (!tasks.value.find(x => x.task_id === t.task_id)) tasks.value.unshift(t)
    } catch (err) { console.warn('SSE task_new 解析失败:', err) }
  })
  es.addEventListener('task_update', (e: MessageEvent) => {
    try {
      const t = JSON.parse(e.data)
      const idx = tasks.value.findIndex(x => x.task_id === t.task_id)
      if (idx >= 0) tasks.value[idx] = { ...tasks.value[idx], ...t }
    } catch (err) { console.warn('SSE task_update 解析失败:', err) }
  })
  es.addEventListener('task_done', (e: MessageEvent) => {
    try {
      const t = JSON.parse(e.data)
      tasks.value = tasks.value.filter(x => x.task_id !== t.task_id)
      if (!doneTasks.value.find(x => x.task_id === t.task_id)) {
        doneTasks.value.unshift(t)
        // 限制已完成任务列表最多 100 条，防止无限增长
        if (doneTasks.value.length > 100) {
          doneTasks.value = doneTasks.value.slice(0, 100)
        }
      }
    } catch (err) { console.warn('SSE task_done 解析失败:', err) }
  })
  es.addEventListener('node_update', (e: MessageEvent) => {
    try {
      const { task_id, node_id, status, attempt } = JSON.parse(e.data)
      const task = tasks.value.find(x => x.task_id === task_id)
      if (task) { const node = task.nodes.find(n => n.node_id === node_id); if (node) { node.status = status; node.attempt = attempt } }
    } catch (err) { console.warn('SSE node_update 解析失败:', err) }
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
