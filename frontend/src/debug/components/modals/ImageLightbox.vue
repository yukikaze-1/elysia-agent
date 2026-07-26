<script setup lang="ts">
const props = defineProps<{ src: string; visible: boolean }>()
const emit = defineEmits<{ close: [] }>()
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="lightbox-overlay" @click="emit('close')">
      <img :src="src" class="lightbox-img" @click.stop />
    </div>
  </Teleport>
</template>

<style scoped>
.lightbox-overlay { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,0.85); display: flex; align-items: center; justify-content: center; cursor: pointer; }
.lightbox-img { max-width: 90vw; max-height: 90vh; object-fit: contain; border-radius: 4px; cursor: default; }
</style>
