<script setup lang="ts">
import { ref, computed } from 'vue'

const props = withDefaults(defineProps<{ text: string; maxLength?: number }>(), { maxLength: 120 })
const expanded = ref(false)

/** 按 codepoint 截断，避免在 emoji / CJK 代理对中间切断 */
function sliceByCodepoints(s: string, max: number): string {
  const chars = [...s]
  if (chars.length <= max) return s
  return chars.slice(0, max).join('') + '...'
}

const display = computed(() => {
  if (!props.text) return ''
  if (expanded.value) return props.text
  return sliceByCodepoints(props.text, props.maxLength)
})
const canExpand = computed(() => props.text && [...props.text].length > props.maxLength)
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
