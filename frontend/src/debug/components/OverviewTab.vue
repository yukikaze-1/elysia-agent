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
