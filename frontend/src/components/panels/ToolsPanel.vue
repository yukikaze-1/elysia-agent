<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useToolsStore } from '@/stores/toolsStore'
import type { PluginInfo } from '@/types/protocol'

const toolsStore = useToolsStore()
const open = ref(false)
const reloadStatus = ref<{ name: string; ok: boolean } | null>(null)

onMounted(() => {
  toolsStore.loadPlugins()
})

function togglePanel() {
  open.value = !open.value
  if (open.value) {
    toolsStore.loadPlugins()
  }
}

function closePanel() {
  open.value = false
}

async function handleReload(name: string) {
  reloadStatus.value = null
  await toolsStore.reloadPlugin(name)
  const plugin = toolsStore.plugins.find(p => p.name === name)
  reloadStatus.value = { name, ok: !!plugin }
  setTimeout(() => { reloadStatus.value = null }, 3000)
}

function safetyLabel(plugin: PluginInfo): string {
  const labels: string[] = []
  if (plugin.safety?.requires_human_approval) labels.push('需审批')
  if (plugin.safety?.network_allowed) labels.push('网络')
  if (plugin.safety?.filesystem_allowed) labels.push('文件系统')
  if (plugin.safety?.max_concurrency && plugin.safety.max_concurrency > 1) {
    labels.push(`并发:${plugin.safety.max_concurrency}`)
  }
  return labels.length ? labels.join(' | ') : '无限制'
}
</script>

<template>
  <div class="tools-panel-wrapper">
    <!-- Trigger button -->
    <button
      class="tools-toggle-btn"
      :class="{ active: open }"
      title="工具管理"
      @click="togglePanel"
    >
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
      </svg>
    </button>

    <!-- Panel -->
    <Transition name="slide">
      <div v-if="open" class="tools-panel" @click.stop>
        <div class="tools-panel-header">
          <span class="tools-panel-title">工具插件</span>
          <button class="tools-panel-close" @click="closePanel">&times;</button>
        </div>

        <div v-if="toolsStore.loading" class="tools-panel-loading">
          <span class="tools-spinner" /> 加载中...
        </div>

        <div v-else-if="toolsStore.plugins.length === 0" class="tools-panel-empty">
          暂无可用工具插件
        </div>

        <div v-else class="tools-panel-list">
          <div
            v-for="plugin in toolsStore.plugins"
            :key="plugin.name"
            class="plugin-card"
          >
            <div class="plugin-card-header">
              <span class="plugin-name">{{ plugin.name }}</span>
              <button
                class="plugin-reload-btn"
                title="重新加载"
                @click="handleReload(plugin.name)"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="23 4 23 10 17 10" />
                  <polyline points="1 20 1 14 7 14" />
                  <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
                </svg>
              </button>
            </div>

            <p v-if="plugin.description" class="plugin-desc">
              {{ plugin.description }}
            </p>

            <div v-if="plugin.capabilities && plugin.capabilities.length" class="plugin-capabilities">
              <span
                v-for="cap in plugin.capabilities"
                :key="cap"
                class="plugin-cap-tag"
              >{{ cap }}</span>
            </div>

            <div class="plugin-safety">{{ safetyLabel(plugin) }}</div>

            <Transition name="fade-status">
              <div
                v-if="reloadStatus && reloadStatus.name === plugin.name"
                class="plugin-reload-status"
                :class="{ ok: reloadStatus.ok, err: !reloadStatus.ok }"
              >
                {{ reloadStatus.ok ? '重新加载成功' : '重新加载失败' }}
              </div>
            </Transition>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.tools-toggle-btn {
  position: fixed;
  top: 16px;
  right: 16px;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
  z-index: 10;
}

.tools-toggle-btn:hover {
  color: var(--text);
  border-color: var(--accent);
}

.tools-toggle-btn.active {
  color: var(--accent);
  border-color: var(--accent);
  background: rgba(233, 69, 96, 0.08);
}

.tools-panel {
  position: fixed;
  top: 52px;
  right: 60px;
  width: 340px;
  max-height: 480px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  z-index: 20;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.tools-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
}

.tools-panel-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

.tools-panel-close {
  width: 28px;
  height: 28px;
  border-radius: 6px;
  border: none;
  background: transparent;
  color: var(--muted);
  font-size: 18px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
}

.tools-panel-close:hover {
  color: var(--text);
  background: rgba(255, 255, 255, 0.05);
}

.tools-panel-loading,
.tools-panel-empty {
  padding: 32px 16px;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
}

.tools-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  vertical-align: middle;
  margin-right: 6px;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.tools-panel-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.plugin-card {
  padding: 12px;
  border-radius: 8px;
  margin-bottom: 6px;
  background: var(--bg);
  border: 1px solid transparent;
  transition: border-color 0.2s;
}

.plugin-card:hover {
  border-color: var(--border);
}

.plugin-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.plugin-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}

.plugin-reload-btn {
  width: 28px;
  height: 28px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
}

.plugin-reload-btn:hover {
  color: var(--accent);
  border-color: var(--accent);
  background: rgba(233, 69, 96, 0.06);
}

.plugin-desc {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
  margin: 0 0 8px;
}

.plugin-capabilities {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 8px;
}

.plugin-cap-tag {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(0, 255, 196, 0.08);
  color: var(--idle, #00ffc4);
  border: 1px solid rgba(0, 255, 196, 0.15);
  white-space: nowrap;
}

.plugin-safety {
  font-size: 11px;
  color: var(--muted);
}

.plugin-reload-status {
  font-size: 11px;
  padding: 4px 8px;
  border-radius: 4px;
  margin-top: 6px;
  text-align: center;
}

.plugin-reload-status.ok {
  color: #4ade80;
  background: rgba(74, 222, 128, 0.08);
}

.plugin-reload-status.err {
  color: var(--accent);
  background: rgba(233, 69, 96, 0.08);
}

/* Transitions */
.slide-enter-active,
.slide-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}

.slide-enter-from {
  opacity: 0;
  transform: translateY(-8px) scale(0.96);
}

.slide-leave-to {
  opacity: 0;
  transform: translateY(-8px) scale(0.96);
}

.fade-status-enter-active,
.fade-status-leave-active {
  transition: opacity 0.3s ease;
}

.fade-status-enter-from,
.fade-status-leave-to {
  opacity: 0;
}
</style>
