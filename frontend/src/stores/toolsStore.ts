import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { PluginInfo } from '@/types/protocol'

export const useToolsStore = defineStore('tools', () => {
  const plugins = ref<PluginInfo[]>([])
  const loading = ref(false)

  async function loadPlugins() {
    loading.value = true
    try {
      const res = await fetch('/tools')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data: PluginInfo[] = await res.json()
      plugins.value = data
    } catch {
      plugins.value = []
    } finally {
      loading.value = false
    }
  }

  async function reloadPlugin(name: string) {
    try {
      const res = await fetch('/tools/reload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await loadPlugins()
    } catch {
      // silently fail — UI can check loading/plugins
    }
  }

  return {
    plugins,
    loading,
    loadPlugins,
    reloadPlugin,
  }
})
