<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{ src: string; visible: boolean }>()
const emit = defineEmits<{ close: [] }>()

const loading = ref(true)
const error = ref(false)

watch(() => props.visible, (v) => {
  if (v) {
    loading.value = true
    error.value = false
    document.addEventListener('keydown', onKeydown)
  } else {
    document.removeEventListener('keydown', onKeydown)
  }
})

function onLoad() {
  loading.value = false
}

function onError() {
  loading.value = false
  error.value = true
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    emit('close')
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="lightbox-overlay" @click="emit('close')">
      <div v-if="loading" class="lightbox-status">加载中...</div>
      <div v-else-if="error" class="lightbox-status error">图片加载失败</div>
      <img
        v-show="!loading && !error"
        :src="src"
        alt="图片预览"
        class="lightbox-img"
        @click.stop
        @load="onLoad"
        @error="onError"
      />
    </div>
  </Teleport>
</template>

<style scoped>
.lightbox-overlay { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,0.85); display: flex; align-items: center; justify-content: center; cursor: pointer; }
.lightbox-img { max-width: 90vw; max-height: 90vh; object-fit: contain; border-radius: 4px; cursor: default; }
.lightbox-status { color: var(--debug-muted); font-size: 14px; }
.lightbox-status.error { color: var(--debug-red); }
</style>
