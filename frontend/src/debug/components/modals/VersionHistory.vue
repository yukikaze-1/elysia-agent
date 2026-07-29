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

// 监听 visible 和 contentType 两个维度：
// 打开弹窗 或 切换 content type（l2 ↔ c6）时都重新加载数据
watch(() => [props.visible, props.contentType], ([v]) => { if (v) load() })

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
