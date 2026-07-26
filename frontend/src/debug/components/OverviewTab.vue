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
const toolsExpanded = ref(false)

function openLightbox(src: string) { lightboxSrc.value = src }

function openVersions(type: 'l2' | 'c6') { versionType.value = type; showVersions.value = true }

// Memory type color
function memoryColor(t: string) {
  const m: Record<string, string> = { fact: '#4caf50', plan: '#2196f3', preference: '#9c27b0', event: '#ff9800' }
  return m[t.toLowerCase()] || '#888'
}

function awarenessCooldown(lastTime?: number): string {
  if (!lastTime) return 'N/A'
  const minutes = Math.round((Date.now() / 1000 - lastTime) / 60)
  return minutes < 60 ? `${Math.max(0, minutes)}min` : 'N/A'
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

const shownTools = computed(() => {
  if (!d.value?.tools) return []
  return toolsExpanded.value ? d.value.tools : d.value.tools.slice(0, 4)
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

    <!-- 10. LTM Recent -->
    <div class="card">
      <h2>LTM Recent</h2>
      <div v-if="!d.lt_recent?.length" class="status">暂无</div>
      <div v-for="(m, i) in d.lt_recent" :key="i" class="list-item">
        <span class="ltm-tag" :style="{ background: memoryColor(m.memory_type) }">{{ m.memory_type }}</span>
        <ExpandableText :text="m.text" :maxLength="120" />
        <span class="label" style="margin-left: auto; flex-shrink: 0">{{ m.poignancy?.toFixed(1) }}</span>
      </div>
    </div>

    <!-- 11. Tools -->
    <div class="card">
      <h2>Tools ({{ d.tools?.length || 0 }})</h2>
      <div v-if="d.currently_executing?.length" class="exec-banner">
        Executing: {{ d.currently_executing.join(', ') }}
      </div>
      <div v-if="!d.tools?.length" class="status">暂无</div>
      <div v-for="t in shownTools" :key="t.name" class="list-item">
        <span style="font-weight: 600; flex-shrink: 0">{{ t.name }}</span>
        <span class="label">{{ t.description?.slice(0, 60) }}</span>
      </div>
      <button v-if="d.tools?.length > 4" class="expand-btn" @click="toolsExpanded = !toolsExpanded">
        {{ toolsExpanded ? '收起' : `展开全部 (${d.tools.length})` }}
      </button>
    </div>

    <!-- 12. Tool History -->
    <div class="card">
      <h2>Tool History</h2>
      <div v-if="!d.tool_history?.length" class="status">暂无</div>
      <div v-for="(h, i) in d.tool_history" :key="i" class="list-item">
        <span style="font-weight: 600; flex-shrink: 0">{{ h.name }}</span>
        <span class="label">{{ typeof h.args === 'string' ? h.args.slice(0, 40) : JSON.stringify(h.args)?.slice(0, 40) }}</span>
        <span v-if="h.error" class="fail" style="margin-left: auto; flex-shrink: 0">✗</span>
        <span v-else class="label" style="margin-left: auto; flex-shrink: 0">{{ typeof h.result === 'string' ? h.result.slice(0, 30) : '' }}</span>
      </div>
    </div>

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

    <!-- 14. Memory Reflector -->
    <div v-if="memReflectorProps?.exists" class="card">
      <h2>Memory Reflector</h2>
      <div class="row"><span class="label">Rounds</span><span>{{ memReflectorProps.round_count }} / {{ memReflectorProps.round_threshold }}</span></div>
      <div class="bar-bg"><div class="bar-fill" :style="{ width: Math.min(100, memReflectorProps.round_count / memReflectorProps.round_threshold * 100) + '%' }"></div></div>
      <div class="row"><span class="label">New Memories</span><span>{{ memReflectorProps.new_memory_count }} / {{ memReflectorProps.memory_threshold }}</span></div>
      <div class="bar-bg"><div class="bar-fill" :style="{ width: Math.min(100, memReflectorProps.new_memory_count / memReflectorProps.memory_threshold * 100) + '%' }"></div></div>
      <div class="row"><span class="label">Locked</span><span :class="memReflectorProps.locked ? 'fail' : 'ok'">{{ memReflectorProps.locked ? 'Yes' : 'No' }}</span></div>
      <details v-if="memReflectorProps.last_summary" class="raw-details">
        <summary>Last Summary</summary>
        <pre>{{ JSON.stringify(memReflectorProps.last_summary, null, 2) }}</pre>
      </details>
    </div>

    <!-- 15. Character Reflector -->
    <div v-if="charReflectorProps?.exists" class="card">
      <h2>Character Reflector</h2>
      <div class="row"><span class="label">Buffer</span><span>{{ charReflectorProps.buffer_size }}</span></div>
      <div class="row"><span class="label">Last Reflection</span><span>{{ charReflectorProps.last_reflection_at ? new Date(charReflectorProps.last_reflection_at * 1000).toLocaleString() : 'N/A' }}</span></div>
      <div class="row"><span class="label">Locked</span><span :class="charReflectorProps.locked ? 'fail' : 'ok'">{{ charReflectorProps.locked ? 'Yes' : 'No' }}</span></div>
      <div class="row" v-if="charReflectorProps.last_duration_ms != null">
        <span class="label">Duration</span><span>{{ charReflectorProps.last_duration_ms }}ms</span>
      </div>
      <div class="row" v-if="charReflectorProps.last_success != null">
        <span class="label">Success</span><span :class="charReflectorProps.last_success ? 'ok' : 'fail'">{{ charReflectorProps.last_success ? '✓' : '✗' }}</span>
      </div>
      <details v-if="charReflectorProps.self_perception_history?.length" class="raw-details">
        <summary>Self Perception History ({{ charReflectorProps.self_perception_history.length }})</summary>
        <pre>{{ JSON.stringify(charReflectorProps.self_perception_history, null, 2) }}</pre>
      </details>
      <details v-if="charReflectorProps.last_prompt_system" class="raw-details">
        <summary>Last Prompt</summary>
        <pre>{{ charReflectorProps.last_prompt_system }}</pre>
      </details>
      <details v-if="charReflectorProps.last_prompt_user" class="raw-details">
        <summary>Last Response</summary>
        <pre>{{ charReflectorProps.last_prompt_user }}</pre>
      </details>
    </div>

    <!-- 16. Character Engine -->
    <div v-if="charEngineProps?.exists" class="card">
      <h2>Character Engine</h2>
      <div class="row"><span class="label">Mood</span><span>{{ charEngineProps.mood || 'N/A' }}</span></div>
      <div class="row"><span class="label">C9 Cooldown</span><span>{{ awarenessCooldown(charEngineProps.last_awareness_time) }}</span></div>
      <ExpandableText :text="charEngineProps.self_l2" :maxLength="120" />
      <ExpandableText :text="charEngineProps.state_l3" :maxLength="120" />
      <div class="row">
        <span class="label">L2 Version</span>
        <a href="#" class="version-link" @click.prevent="openVersions('l2')">{{ charEngineProps.l2_version_count }}</a>
      </div>
      <div class="row">
        <span class="label">C6 Version</span>
        <a href="#" class="version-link" @click.prevent="openVersions('c6')">{{ charEngineProps.c6_version_count }}</a>
      </div>
      <details v-if="charEngineProps.state_history?.length" class="raw-details">
        <summary>State History ({{ charEngineProps.state_history.length }})</summary>
        <pre>{{ JSON.stringify(charEngineProps.state_history, null, 2) }}</pre>
      </details>
    </div>
  </div>

  <!-- Modals -->
  <ImageLightbox v-if="lightboxSrc" :src="lightboxSrc" @close="lightboxSrc = ''" />
  <PromptViewer v-if="promptIndex >= 0" :index="promptIndex" @close="promptIndex = -1" />
  <ResponseViewer v-if="responseIndex >= 0" :index="responseIndex" @close="responseIndex = -1" />
  <VersionHistory v-if="showVersions" :type="versionType" @close="showVersions = false" />
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
.session-msg { padding: 3px 0; border-bottom: 1px solid rgba(255,255,255,0.04); font-size: 12px; display: flex; gap: 6px; align-items: baseline; }
.role-tag { font-size: 9px; padding: 1px 5px; border-radius: 3px; color: #fff; font-weight: 600; white-space: nowrap; flex-shrink: 0; }
.msg-thumb { max-width: 60px; max-height: 60px; border-radius: 4px; cursor: pointer; border: 1px solid var(--debug-border); margin: 2px 0; }
.list-item { padding: 3px 0; border-bottom: 1px solid rgba(255,255,255,0.04); font-size: 12px; display: flex; gap: 6px; align-items: baseline; }
.bar-bg { width: 100%; height: 4px; background: var(--debug-border); border-radius: 2px; margin: 2px 0 4px; }
.bar-fill { height: 100%; background: var(--debug-accent); border-radius: 2px; transition: width 0.3s; }
.exec-banner { background: var(--debug-red); color: #fff; padding: 2px 8px; border-radius: 3px; font-size: 11px; margin: 4px 0; }
.version-link { color: var(--debug-accent); text-decoration: none; cursor: pointer; }
.version-link:hover { text-decoration: underline; }
.expand-btn { background: none; border: 1px solid var(--debug-border); border-radius: 3px; color: var(--debug-accent); cursor: pointer; font-size: 11px; padding: 2px 8px; margin-top: 4px; }
</style>
