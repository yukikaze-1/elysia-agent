<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useAbortController } from '@/composables/useAbortController'
import type { CustomProvider, ProvidersResponse, UnconfiguredEntry } from '@/types/protocol'

const props = defineProps<{
  service: 'llm' | 'stt' | 'tts'
  currentModel: string
  /** 服务未连接（如 STT 后端没启动）时禁用下拉，避免选一个不可用的模型。 */
  disabled?: boolean
  /** 后端默认模型（capabilities 下发）；非空时在下拉下方显示「当前/默认」提示。 */
  defaultModel?: string
}>()

const emit = defineEmits<{
  switch: [modelId: string]
  'open-config': []
}>()

const options = ref<{ value: string; label: string }[]>([])
const loading = ref(false)
const unconfigured = ref<UnconfiguredEntry[]>([])
const { signal } = useAbortController()

async function refreshProviders() {
  await loadProviders()
}

async function loadProviders() {
  loading.value = true
  try {
    const res = await fetch('/api/providers', { signal: signal() })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data: ProvidersResponse = await res.json()

    const registry = data[props.service] as Record<string, { models: string[] }>
    const customEntries: CustomProvider[] = data.custom[props.service] ?? []

    const opts: { value: string; label: string }[] = []

    for (const [provider, info] of Object.entries(registry)) {
      if (!info.models || info.models.length === 0) continue
      for (const m of info.models) {
        const value = `${provider}/${m}`
        opts.push({ value, label: value })
      }
    }
    // 自定义 provider 从顶层 custom 键合并（custom 不在 data[service] 里）
    for (const entry of customEntries) {
      const value = `custom/${entry.name}`
      opts.push({ value, label: value })
    }

    options.value = opts
    unconfigured.value = data.unconfigured[props.service] ?? []
  } catch (e) {
    if ((e as Error).name === 'AbortError') return
    console.warn('加载 provider 列表失败', e)
  } finally {
    loading.value = false
  }
}

onMounted(loadProviders)

// 暴露 refresh 方法供父组件在添加自定义端点后调用
defineExpose({ refreshProviders })

function onChange(event: Event) {
  const select = event.target as HTMLSelectElement
  const value = select.value
  if (value) {
    emit('switch', value)
  }
}
</script>

<template>
  <!-- 容器包裹：select + unconf-hint 纵向堆叠。父级 .setting-row 是 flex 行，
       若 hint 直接作为 flex 兄弟会横向挤占 select（长提示文本把下拉压扁）。 -->
  <div class="model-selector">
    <select
      :value="disabled ? '' : currentModel"
      :disabled="disabled"
      class="model-select"
      :class="{ 'is-disabled': disabled }"
      @change="onChange"
    >
      <option value="" disabled>{{ disabled ? '未连接' : '选择模型...' }}</option>
      <template v-if="!disabled">
        <option
          v-for="opt in options"
          :key="opt.value"
          :value="opt.value"
        >
          {{ opt.label }}
        </option>
      </template>
    </select>
    <div
      v-if="unconfigured.length > 0"
      class="unconf-hint"
    >
      <span>还有 {{ unconfigured.length }} 个 provider 未配置（{{ unconfigured.map(e => e.name + ' → ' + e.env_vars.join('/')).join('、') }}）</span>
      <button class="unconf-link" @click="emit('open-config')">查看配置方式</button>
    </div>
    <div
      v-if="defaultModel"
      class="model-hint"
    >当前 {{ currentModel || '—' }} · 默认 {{ defaultModel }}</div>
  </div>
</template>

<style scoped>
/* flex:1 占满 setting-row 剩余宽度；min-width:0 允许收缩，防长内容撑破行 */
.model-selector { flex: 1; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.model-select {
  width: 100%;
  padding: 6px 10px;
  border-radius: var(--radius-sm, 8px);
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
  outline: none;
  transition: border-color 0.2s;
}
.model-select:focus {
  border-color: var(--accent);
}
.model-select.is-disabled {
  color: var(--muted);
  cursor: not-allowed;
  opacity: 0.6;
}
.unconf-hint { font-size: 11px; color: var(--muted); margin-top: 4px; display: flex; gap: 6px; align-items: center; }
.unconf-link { font-size: 11px; color: var(--accent); background: none; border: none; cursor: pointer; padding: 0; }
.model-hint { font-size: 11px; color: var(--muted); margin-top: 4px; }
</style>
