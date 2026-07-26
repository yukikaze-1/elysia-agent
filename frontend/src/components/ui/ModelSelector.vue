<script setup lang="ts">
import { ref, onMounted } from 'vue'

const props = defineProps<{
  service: 'llm' | 'stt' | 'tts'
  currentModel: string
}>()

const emit = defineEmits<{
  switch: [modelId: string]
}>()

interface ProviderInfo {
  models: string[]
}

interface CustomEntry {
  name: string
  base_url: string
  api_key_env?: string
  models?: string[]
}

interface ProvidersResponse {
  llm: Record<string, ProviderInfo>
  stt: Record<string, ProviderInfo>
  tts: Record<string, ProviderInfo>
  custom?: {
    llm?: CustomEntry[]
    stt?: CustomEntry[]
    tts?: CustomEntry[]
  }
}

const options = ref<{ value: string; label: string }[]>([])

onMounted(async () => {
  try {
    const res = await fetch('/api/providers')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data: ProvidersResponse = await res.json()

    const registry = data[props.service]
    const customEntries = (data.custom?.[props.service] as CustomEntry[] | undefined) ?? []

    const opts: { value: string; label: string }[] = []

    for (const [provider, info] of Object.entries(registry)) {
      if (!info.models || info.models.length === 0) continue
      const models = provider === 'custom'
        ? customEntries.map(e => e.name)
        : info.models
      for (const m of models) {
        const value = `${provider}/${m}`
        opts.push({ value, label: value })
      }
    }

    options.value = opts
  } catch (e) {
    console.warn('加载 provider 列表失败', e)
  }
})

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
    :value="currentModel"
    class="model-select"
    @change="onChange"
  >
    <option value="" disabled>选择模型...</option>
    <option
      v-for="opt in options"
      :key="opt.value"
      :value="opt.value"
    >
      {{ opt.label }}
    </option>
  </select>
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
</style>
