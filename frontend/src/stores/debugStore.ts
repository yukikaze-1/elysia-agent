import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import type { DebugData } from '@/debug/types'
import { debugFetch } from '@/debug/auth'

export const useDebugStore = defineStore('debug', () => {
  const data = ref<DebugData | null>(null)
  const activeTab = ref<'overview' | 'database' | 'tasks'>('overview')
  const paused = ref(false)
  const error = ref<string | null>(null)

  const ltmPage = ref({ limit: 50, offset: 0 })
  const archivePage = ref({ limit: 100, offset: 0 })

  let pollTimer: ReturnType<typeof setInterval> | null = null
  let pollInterval = 2000
  let _abortController: AbortController | null = null

  async function fetchDebugData() {
    try {
      _abortController?.abort()
      _abortController = new AbortController()
      const res = await debugFetch('/debug/api', { signal: _abortController.signal })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      data.value = await res.json()
      error.value = null
      pollInterval = 2000 // 成功时重置退避
    } catch (e) {
      if ((e as Error).name === 'AbortError') return
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
    // 防御性清理：避免 stopPoll 后被其他地方意外创建重复定时器
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
    fetchDebugData()
    pollTimer = setInterval(fetchDebugData, pollInterval)
  }

  function stopPoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
    _abortController?.abort(); _abortController = null
    pollInterval = 2000
  }

  /** 凭据变更后立刻重试一次，不用等退避计时（S2-F3 闸门「保存并重试」）。 */
  function retryNow() {
    stopPoll()   // 同时复位退避间隔并中止在途请求
    if (activeTab.value === 'overview' && !paused.value) startPoll()
    else fetchDebugData()   // 非概览页也要探一次，让 401 判定刷新
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

  return { data, activeTab, paused, error, ltmPage, archivePage, fetchDebugData, startPoll, stopPoll, retryNow }
})
