import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { PluginInfo } from '@/types/protocol'

export const useToolsStore = defineStore('tools', () => {
  const plugins = ref<PluginInfo[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function loadPlugins() {
    loading.value = true
    error.value = null
    try {
      const res = await fetch('/tools')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data: PluginInfo[] = await res.json()
      plugins.value = data
    } catch (e) {
      // 保留上次数据——把网络故障渲染成「暂无工具插件」会误导用户（S8/S10）
      error.value = e instanceof Error ? e.message : '加载工具插件失败'
    } finally {
      loading.value = false
    }
  }

  async function reloadPlugin(name: string): Promise<boolean> {
    try {
      const res = await fetch('/tools/reload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await loadPlugins()
      return true
    } catch (e) {
      // 失败必须显式回流给 UI——否则 ToolsPanel 会把它兜成「重新加载成功」（S3）
      error.value = e instanceof Error ? e.message : `重载插件失败: ${name}`
      return false
    }
  }

  return {
    plugins,
    loading,
    error,
    loadPlugins,
    reloadPlugin,
  }
})
