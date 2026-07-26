<script setup lang="ts">
import { ref, computed } from 'vue'

const props = withDefaults(defineProps<{ text: string; maxLength?: number }>(), { maxLength: 120 })
const expanded = ref(false)

const display = computed(() => {
  if (!props.text) return ''
  if (expanded.value || props.text.length <= props.maxLength) return props.text
  return props.text.slice(0, props.maxLength) + '...'
})
const canExpand = computed(() => props.text && props.text.length > props.maxLength)
</script>

<template>
  <span>
    <span>{{ display }}</span>
    <button v-if="canExpand" class="expand-btn" @click="expanded = !expanded">
      {{ expanded ? '收起' : '展开' }}
    </button>
  </span>
</template>

<style scoped>
.expand-btn {
  background: none; border: 1px solid var(--debug-border); border-radius: 3px;
  color: var(--debug-accent); cursor: pointer; font-size: 11px; padding: 0 4px;
  margin-left: 4px; vertical-align: middle;
}
</style>
