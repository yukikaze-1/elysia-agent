<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

interface DebugData {
  fsm_state: string
  mood: string
  mood_intensity: number
  idle_monitor: any
  [key: string]: any
}

const data = ref<DebugData | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null

async function fetchDebug() {
  try {
    const res = await fetch('/debug/api')
    if (res.ok) data.value = await res.json()
  } catch { /* silent */ }
}

onMounted(() => {
  fetchDebug()
  pollTimer = setInterval(fetchDebug, 2000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <div class="debug-container">
    <h1>Elysia Debug</h1>
    <p class="sub">实时调试仪表盘 — 每 2 秒自动刷新</p>
    <div class="grid">
      <div class="card" v-if="data">
        <h2>System</h2>
        <p>FSM: <strong>{{ data.fsm_state }}</strong></p>
        <p>Mood: {{ data.mood }} ({{ Math.round((data.mood_intensity || 0) * 100) }}%)</p>
        <p>Idle: {{ data.idle_monitor?.state || 'N/A' }}</p>
      </div>
      <div class="card" v-if="data">
        <h2>Memory</h2>
        <p>Session: {{ data.memory?.session_count || '—' }}</p>
        <p>LTM: {{ data.memory?.long_term_count || '—' }}</p>
        <p>DB: {{ data.health?.db || '—' }}</p>
      </div>
      <div class="card" v-if="data">
        <h2>Latency</h2>
        <p>{{ data.latency ? JSON.stringify(data.latency) : '—' }}</p>
      </div>
      <div class="card" v-if="data">
        <h2>Process Memory</h2>
        <p>{{ data.process_memory || '—' }}</p>
      </div>
      <div class="card" v-if="data">
        <h2>Scheduler</h2>
        <p>{{ data.scheduler ? JSON.stringify(data.scheduler) : '—' }}</p>
      </div>
      <div class="card" v-if="data">
        <h2>Token</h2>
        <p>{{ data.token_stats ? data.token_stats.total + ' tokens' : '—' }}</p>
      </div>
      <div class="card" v-if="data">
        <h2>LLM Prompt</h2>
        <pre>{{ data.llm_prompt || '—' }}</pre>
      </div>
      <div class="card" v-if="data">
        <h2>LLM Response</h2>
        <pre>{{ data.llm_response || '—' }}</pre>
      </div>
      <div class="card" v-if="data">
        <h2>Session Recent</h2>
        <p>{{ data.session_recent_count || '—' }} messages</p>
      </div>
      <div class="card" v-if="data">
        <h2>LTM Recent</h2>
        <p>{{ data.ltm_recent_count || '—' }} memories</p>
      </div>
      <div class="card" v-if="data">
        <h2>Tools</h2>
        <pre>{{ data.tools ? JSON.stringify(data.tools, null, 2) : '—' }}</pre>
      </div>
      <div class="card" v-if="data">
        <h2>Tool History</h2>
        <p>{{ data.tool_history_count || '—' }} entries</p>
      </div>
      <div class="card" v-if="data">
        <h2>Consolidation</h2>
        <pre>{{ data.consolidation ? JSON.stringify(data.consolidation, null, 2) : '—' }}</pre>
      </div>
      <div class="card" v-if="data">
        <h2>Memory Reflector</h2>
        <pre>{{ data.memory_reflector ? JSON.stringify(data.memory_reflector, null, 2) : '—' }}</pre>
      </div>
      <div class="card" v-if="data">
        <h2>Character Reflector</h2>
        <pre>{{ data.character_reflector ? JSON.stringify(data.character_reflector, null, 2) : '—' }}</pre>
      </div>
      <div class="card" v-if="data">
        <h2>CharacterEngine</h2>
        <pre>{{ data.character_engine ? JSON.stringify(data.character_engine, null, 2) : '—' }}</pre>
      </div>
    </div>
  </div>
</template>

<style>
:root {
  --debug-bg: #0f0f1a; --debug-surface: #1a1a2e; --debug-border: #2a2a4a;
  --debug-text: #ddd; --debug-muted: #888; --debug-accent: #e94560;
}
body { font-family: 'Segoe UI', system-ui, monospace; background: var(--debug-bg); color: var(--debug-text); padding: 20px; max-width: 1100px; margin: 0 auto; font-size: 13px; }
h1 { font-size: 18px; margin-bottom: 4px; }
.sub { color: var(--debug-muted); font-size: 12px; margin-bottom: 16px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.card { background: var(--debug-surface); border: 1px solid var(--debug-border); border-radius: 8px; padding: 12px 14px; overflow: hidden; }
.card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 1px; color: var(--debug-accent); margin-bottom: 8px; border-bottom: 1px solid var(--debug-border); padding-bottom: 4px; }
.card p { margin: 2px 0; font-size: 12px; }
.card pre { font-size: 11px; white-space: pre-wrap; word-break: break-all; max-height: 200px; overflow-y: auto; margin: 4px 0; }
</style>
