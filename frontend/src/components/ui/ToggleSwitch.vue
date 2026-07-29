<script setup lang="ts">
const props = defineProps<{ modelValue: boolean; ariaLabel?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    emit('update:modelValue', !props.modelValue)
  }
}
</script>

<template>
  <div
    :class="['toggle', { on: modelValue }]"
    role="switch"
    :aria-checked="modelValue"
    :aria-label="ariaLabel || '开关'"
    tabindex="0"
    @click="emit('update:modelValue', !modelValue)"
    @keydown="handleKeydown"
  ></div>
</template>

<style scoped>
.toggle {
  width: 40px;
  height: 22px;
  border-radius: 11px;
  background: var(--border);
  position: relative;
  cursor: pointer;
  transition: background 0.3s;
  flex-shrink: 0;
}
.toggle.on {
  background: var(--accent);
}
.toggle::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: white;
  transition: left 0.3s;
}
.toggle.on::after {
  left: 20px;
}
</style>
