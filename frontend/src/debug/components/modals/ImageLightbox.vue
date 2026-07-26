<script setup lang="ts">
// Stub — will be implemented in a later task
defineProps<{ src?: string; alt?: string }>()
const emit = defineEmits<{ close: [] }>()
</script>

<template>
  <Teleport to="body">
    <div class="modal-overlay" @click.self="emit('close')">
      <div class="modal-box">
        <img v-if="src" :src="src" :alt="alt" style="max-width: 100%; border-radius: 4px;" />
        <button class="btn" @click="emit('close')">关闭</button>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.modal-overlay { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center; }
.modal-box { background: var(--debug-surface); border: 1px solid var(--debug-border); border-radius: 8px; padding: 20px; max-width: 90vw; box-shadow: 0 4px 20px rgba(0,0,0,0.4); }
.btn { padding: 4px 12px; border: 1px solid var(--debug-border); border-radius: 4px; background: transparent; color: var(--debug-text); cursor: pointer; font-size: 12px; margin-top: 10px; }
</style>
