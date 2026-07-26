<script setup lang="ts">
defineProps<{ time: number }>()
</script>

<template>
  <div class="time-divider">{{ new Date(time).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}</div>
</template>

<style scoped>
.time-divider {
  text-align: center;
  font-size: 11px;
  color: var(--muted);
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
  margin-bottom: 8px;
}
</style>
