<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useAbortController } from '@/composables/useAbortController'
import type { ProvidersResponse, UnconfiguredEntry } from '@/types/protocol'

const props = defineProps<{
  service: 'llm' | 'stt' | 'tts'
  currentModel: string
  /** 服务未连接（如 STT 后端没启动）时禁用下拉，避免选一个不可用的模型。 */
  disabled?: boolean
}>()

const emit = defineEmits<{
  switch: [modelId: string]
  'open-config': []
}>()

interface CustomEntry {
  name: string
  base_url: string
  api_key_env?: string
  models?: string[]
}

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
    const customEntries = (data.custom?.[props.service] as CustomEntry[] | undefined) ?? []

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
    unconfigured.value = data.unconfigured?.[props.service] ?? []
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
</template>

<style scoped>
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
</style>
