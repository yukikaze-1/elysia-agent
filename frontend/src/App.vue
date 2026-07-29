<script setup lang="ts">
import { onMounted, onUnmounted, ref, provide, watch } from 'vue'
import AppLayout from '@/components/layout/AppLayout.vue'
import { useConnectionStore } from '@/stores/connectionStore'
import { useSettingsStore } from '@/stores/settingsStore'
import { useWebSocket } from '@/composables/useWebSocket'
import { useLightbox } from '@/composables/useLightbox'

const approvalRequest = ref<{ tool: string; toolParams?: string; request_id: string } | null>(null)
provide('approvalRequest', approvalRequest)

useWebSocket(approvalRequest)

// 内心独白开关 → body class 联动
const settingsStore = useSettingsStore()
watch(() => settingsStore.innerVoiceEnabled, (enabled) => {
  document.body.classList.toggle('inner-voice-hidden', !enabled)
}, { immediate: true })

const { lightboxImage, close: closeLightbox } = useLightbox()

// 全局键盘快捷键——Escape：优先关闭灯箱，否则中断 AI 生成
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    if (lightboxImage.value !== null) { closeLightbox(); return }
    useConnectionStore().send({ type: 'interrupt' })
  }
}

onMounted(() => document.addEventListener('keydown', onKeydown))
onUnmounted(() => document.removeEventListener('keydown', onKeydown))
</script>

<template>
  <AppLayout :approvalRequest="approvalRequest" />
</template>
