<script setup lang="ts">
defineProps<{ message: string; visible: boolean }>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="confirm-overlay" @click.self="emit('cancel')">
      <div class="confirm-box">
        <p class="confirm-msg">{{ message }}</p>
        <div class="confirm-actions">
          <button class="confirm-btn cancel" @click="emit('cancel')">取消</button>
          <button class="confirm-btn danger" @click="emit('confirm')">确认</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.confirm-overlay {
  position: fixed; inset: 0; z-index: 9999;
  background: rgba(0,0,0,0.5); display: flex; align-items: center; justify-content: center;
}
.confirm-box {
  background: var(--debug-surface); border: 1px solid var(--debug-border);
  border-radius: 8px; padding: 20px 24px; min-width: 300px; max-width: 420px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.4);
}
.confirm-msg { color: var(--debug-text); font-size: 14px; margin: 0 0 16px; }
.confirm-actions { display: flex; gap: 10px; justify-content: flex-end; }
.confirm-btn {
  padding: 6px 18px; border-radius: 6px; border: 1px solid var(--debug-border);
  background: transparent; color: var(--debug-text); cursor: pointer; font-size: 13px;
}
.confirm-btn.cancel:hover { border-color: var(--debug-muted); }
.confirm-btn.danger { background: var(--debug-accent); color: #fff; border-color: var(--debug-accent); }
.confirm-btn.danger:hover { opacity: 0.85; }
</style>
