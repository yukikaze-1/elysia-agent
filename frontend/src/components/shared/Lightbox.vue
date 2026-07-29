<script setup lang="ts">
import { useLightbox } from '@/composables/useLightbox'

const { lightboxImage, close } = useLightbox()
</script>

<template>
  <!-- v-if 替代 classList 操作，完全由 Vue 响应式控制显示/隐藏 -->
  <div v-if="lightboxImage" id="lightbox" @click.self="close">
    <img id="lightbox-img" :src="lightboxImage" />
  </div>
</template>

<style scoped>
#lightbox { position: fixed; inset: 0; z-index: 9999; display: flex; align-items: center; justify-content: center; background: rgba(0,0,0,0.85); cursor: pointer; }
#lightbox-img { max-width: 90vw; max-height: 90vh; object-fit: contain; border-radius: 4px; }
</style>
