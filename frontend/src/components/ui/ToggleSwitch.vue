<script setup lang="ts">
defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
</script>

<template>
  <div
    :class="['toggle', { on: modelValue }]"
    @click="emit('update:modelValue', !modelValue)"
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
