<script setup lang="ts">
import { onMounted, onUnmounted, ref, provide, watch } from 'vue'
import AppLayout from '@/components/layout/AppLayout.vue'
import { useConnectionStore } from '@/stores/connectionStore'
import { useSettingsStore } from '@/stores/settingsStore'
import { useWebSocket } from '@/composables/useWebSocket'

const approvalRequest = ref<{ tool: string; toolParams?: string; request_id: string } | null>(null)
provide('approvalRequest', approvalRequest)

useWebSocket(approvalRequest)

// 内心独白开关 → body class 联动
const settingsStore = useSettingsStore()
watch(() => settingsStore.innerVoiceEnabled, (enabled) => {
  document.body.classList.toggle('inner-voice-hidden', !enabled)
}, { immediate: true })

// 全局键盘快捷键——Escape 中断 AI 生成
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const el = document.querySelector('.lightbox-hidden')
    if (el) { (el as HTMLElement).classList.add('lightbox-hidden'); return }
    useConnectionStore().send({ type: 'interrupt' })
  }
}

onMounted(() => document.addEventListener('keydown', onKeydown))
onUnmounted(() => document.removeEventListener('keydown', onKeydown))
</script>

<template>
  <AppLayout :approvalRequest="approvalRequest" />
</template>
