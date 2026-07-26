<script setup lang="ts">
import { ref, computed } from 'vue'

const props = defineProps<{ total: number; perPage: number; currentOffset: number }>()
const emit = defineEmits<{ 'update:perPage': [value: number]; goToOffset: [offset: number] }>()

const totalPages = computed(() => Math.max(1, Math.ceil(props.total / props.perPage)))
const currentPage = computed(() => Math.floor(props.currentOffset / props.perPage) + 1)

const jumpInput = ref('')

function goToPage(page: number) {
  const p = Math.max(1, Math.min(page, totalPages.value))
  emit('goToOffset', (p - 1) * props.perPage)
}

function onJump() {
  const n = parseInt(jumpInput.value)
  if (n >= 1) { goToPage(n); jumpInput.value = '' }
}

function onPerPageChange(e: Event) {
  emit('update:perPage', Number((e.target as HTMLSelectElement).value))
}
</script>

<template>
  <div class="pagination">
    <button :disabled="currentPage <= 1" @click="goToPage(currentPage - 1)">←</button>
    <span>第 {{ currentPage }}/{{ totalPages }} 页</span>
    <button :disabled="currentPage >= totalPages" @click="goToPage(currentPage + 1)">→</button>
    <select :value="perPage" @change="onPerPageChange" class="per-page-select">
      <option :value="50">50</option>
      <option :value="100">100</option>
      <option :value="200">200</option>
    </select>
    <span class="jump-label">前往</span>
    <input v-model="jumpInput" class="jump-input" size="4" @keydown.enter="onJump" placeholder="#" />
    <button @click="onJump">跳转</button>
  </div>
</template>

<style scoped>
.pagination { display: flex; align-items: center; gap: 6px; font-size: 12px; padding: 8px 0; color: var(--debug-muted); }
.pagination button {
  padding: 2px 8px; border: 1px solid var(--debug-border); border-radius: 4px;
  background: transparent; color: var(--debug-text); cursor: pointer; font-size: 12px;
}
.pagination button:disabled { opacity: 0.4; cursor: default; }
.pagination button:hover:not(:disabled) { border-color: var(--debug-accent); }
.per-page-select { background: var(--debug-bg); color: var(--debug-text); border: 1px solid var(--debug-border); border-radius: 4px; padding: 2px 4px; font-size: 12px; }
.jump-input { background: var(--debug-bg); color: var(--debug-text); border: 1px solid var(--debug-border); border-radius: 4px; padding: 2px 6px; font-size: 12px; width: 48px; }
.jump-label { font-size: 12px; }
</style>
