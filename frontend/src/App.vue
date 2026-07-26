<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import AppLayout from '@/components/layout/AppLayout.vue'
import { useConnectionStore } from '@/stores/connectionStore'

// 全局键盘快捷键——Escape 中断 AI 生成
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const lb = document.getElementById('lightbox')
    if (lb && !lb.classList.contains('lightbox-hidden')) {
      lb.classList.add('lightbox-hidden')
      return
    }
    useConnectionStore().send({ type: 'interrupt' })
  }
}

onMounted(() => document.addEventListener('keydown', onKeydown))
onUnmounted(() => document.removeEventListener('keydown', onKeydown))
</script>

<template>
  <AppLayout />
</template>
