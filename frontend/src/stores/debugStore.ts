import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import type { DebugData } from '@/debug/types'

export const useDebugStore = defineStore('debug', () => {
  const data = ref<DebugData | null>(null)
  const activeTab = ref<'overview' | 'database' | 'tasks'>('overview')
  const paused = ref(false)
  const error = ref<string | null>(null)

  const ltmPage = ref({ limit: 50, offset: 0 })
  const archivePage = ref({ limit: 100, offset: 0 })

  let pollTimer: ReturnType<typeof setInterval> | null = null
  let pollInterval = 2000

  async function fetchDebugData() {
    try {
      const res = await fetch('/debug/api')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      data.value = await res.json()
      error.value = null
      pollInterval = 2000 // 成功时重置退避
    } catch (e) {
      error.value = (e as Error).message
      pollInterval = Math.min(pollInterval * 2, 30000) // 指数退避到 30s
      restartPollTimer()
    }
  }

  function restartPollTimer() {
    if (pollTimer) clearInterval(pollTimer)
    if (!paused.value) {
      pollTimer = setInterval(fetchDebugData, pollInterval)
    }
  }

  function startPoll() {
    if (paused.value) return
    fetchDebugData()
    pollTimer = setInterval(fetchDebugData, pollInterval)
  }

  function stopPoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
    pollInterval = 2000
  }

  // Tab 切换：切到 overview 开始轮询，切走停止
  watch(activeTab, (tab) => {
    if (tab === 'overview') startPoll()
    else stopPoll()
  })

  // Pause 切换
  watch(paused, (p) => {
    if (p) stopPoll()
    else startPoll()
  })

  // 初始化：默认在 overview tab，自动开始轮询
  startPoll()

  return { data, activeTab, paused, error, ltmPage, archivePage, fetchDebugData, startPoll, stopPoll }
})
