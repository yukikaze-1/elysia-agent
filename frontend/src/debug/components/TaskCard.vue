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
    edges: { smooth: { enabled: true, type: 'cubicBezier', roundness: 0.5 } },
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
