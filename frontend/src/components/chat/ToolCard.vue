<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ToolCallData, ToolCallResult } from '@/types/protocol'

const props = defineProps<{
  toolCall: ToolCallData
  result?: ToolCallResult
}>()

const expanded = ref(false)

function toggle() {
  expanded.value = !expanded.value
}

const statusClass = computed(() => {
  if (!props.result) return 'executing'
  if (props.result.success === false) return 'fail'
  return 'done'
})

const statusText = computed(() => {
  if (!props.result) return 'executing...'
  if (props.result.success === false) return 'failed'
  if (props.result.duration_ms != null) {
    const ms = props.result.duration_ms
    if (ms >= 1000) return `done (${(ms / 1000).toFixed(1)}s)`
    return `done (${ms}ms)`
  }
  return 'done'
})

const argsJson = computed(() => {
  try {
    const parsed = JSON.parse(props.toolCall.function.arguments)
    return JSON.stringify(parsed, null, 2)
  } catch {
    return props.toolCall.function.arguments
  }
})

const argsSummary = computed(() => {
  try {
    const parsed = JSON.parse(props.toolCall.function.arguments)
    const keys = Object.keys(parsed)
    if (keys.length === 0) return '{}'
    return keys.map(k => `${k}: ${summarizeValue(parsed[k])}`).join(', ')
  } catch {
    return props.toolCall.function.arguments
  }
})

function summarizeValue(val: unknown): string {
  if (val == null) return 'null'
  if (typeof val === 'string') {
    if (val.length > 40) return `"${val.slice(0, 40)}..."`
    return `"${val}"`
  }
  if (typeof val === 'object') {
    if (Array.isArray(val)) return `[${val.length} items]`
    return `{${Object.keys(val as Record<string, unknown>).length} keys}`
  }
  return String(val)
}
</script>

<template>
  <div
    class="tool-card"
    :class="[statusClass, { expanded }]"
    @click="toggle"
  >
    <div class="tool-card-header">
      <span class="tool-icon">&#9881;</span>
      <span class="tool-name">{{ toolCall.function.name }}</span>
      <span class="tool-summary">{{ argsSummary }}</span>
      <span class="tool-status">
        <span v-if="statusClass === 'executing'" class="tool-spinner" />
        {{ statusText }}
      </span>
      <span class="tool-card-chevron">&#9660;</span>
    </div>
    <div class="tool-card-body">
      <div class="tool-card-section">
        <div class="label">Arguments</div>
        <div class="value mono">{{ argsJson }}</div>
      </div>
      <div v-if="result?.error" class="tool-card-error">
        {{ result.error }}
      </div>
      <div v-if="result?.content" class="tool-card-section">
        <div class="label">Result</div>
        <div
          class="value"
          :class="{ mono: result.content!.length > 100 }"
        >{{ result.content }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tool-card {
  margin: 4px 0;
  border-radius: var(--radius-sm);
  background: var(--surface);
  border-left: 3px solid var(--thinking);
  font-size: 13px;
  overflow: hidden;
  animation: fadeIn .2s ease;
}

.tool-card.executing {
  border-left-color: var(--thinking);
}

.tool-card.done {
  border-left-color: var(--idle);
}

.tool-card.fail {
  border-left-color: var(--accent);
}

.tool-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  user-select: none;
}

.tool-card-header:hover {
  background: rgba(255, 255, 255, .03);
}

.tool-card-header .tool-icon {
  font-size: 15px;
  flex-shrink: 0;
}

.tool-card-header .tool-name {
  font-weight: 600;
}

.tool-card-header .tool-summary {
  flex: 1;
  font-size: 11px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.tool-card-header .tool-status {
  margin-left: auto;
  font-size: 11px;
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: 4px;
}

.tool-spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid var(--border);
  border-top-color: var(--thinking);
  border-radius: 50%;
  animation: spin .8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.tool-card-body {
  padding: 0 12px 8px;
  display: none;
}

.tool-card.expanded .tool-card-body {
  display: block;
}

.tool-card-args {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 6px;
  word-break: break-all;
}

.tool-card-result {
  font-size: 12px;
  padding: 6px 8px;
  border-radius: 4px;
  background: rgba(0, 0, 0, .2);
  max-height: 120px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.tool-card-result.success {
  border-left: 2px solid var(--idle);
}

.tool-card-result.fail {
  border-left: 2px solid var(--accent);
}

.tool-card-chevron {
  font-size: 12px;
  color: var(--muted);
  transition: transform .2s;
}

.tool-card.expanded .tool-card-chevron {
  transform: rotate(180deg);
}

.tool-card-error {
  font-size: 12px;
  padding: 6px 8px;
  border-radius: 4px;
  background: rgba(233, 69, 96, .1);
  color: var(--accent);
  white-space: pre-wrap;
  word-break: break-all;
  margin-bottom: 6px;
}

.tool-card-section {
  margin-bottom: 6px;
}

.tool-card-section .label {
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 2px;
  text-transform: uppercase;
}

.tool-card-section .value {
  font-size: 12px;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 400px;
  overflow-y: auto;
}

.tool-card-section .value.mono {
  font-family: monospace;
  font-size: 11px;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}
</style>
