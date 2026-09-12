<script setup lang="ts">
import { computed, markRaw, type Component } from 'vue'
import { useDebugStore } from '@/stores/debugStore'
import { authRejected, clearToken, setToken } from './auth'
import OverviewTab from './components/OverviewTab.vue'
import DatabaseTab from './components/DatabaseTab.vue'
import TasksTab from './components/TasksTab.vue'
import TokenGate from './components/TokenGate.vue'

const store = useDebugStore()

const tabs: { key: 'overview' | 'database' | 'tasks'; label: string; comp: Component }[] = [
  { key: 'overview', label: '概览', comp: markRaw(OverviewTab) },
  { key: 'database', label: '数据库', comp: markRaw(DatabaseTab) },
  { key: 'tasks', label: '任务', comp: markRaw(TasksTab) },
]

const activeComp = computed(() => tabs.find(t => t.key === store.activeTab)?.comp)

function switchTab(key: 'overview' | 'database' | 'tasks') {
  store.activeTab = key
}

function togglePause() {
  store.paused = !store.paused
}

// 凭据闸门：只在后端 401 时出现（没配 SERVER_AUTH_TOKEN 的后端不会触发）
function onToken(token: string) {
  if (!setToken(token)) return
  authRejected.value = false   // 乐观收起；下一次请求若仍 401 会重新弹出
  store.retryNow()
}

function onClearToken() {
  clearToken()
  store.retryNow()
}

async function copyJson(indent: number) {
  if (!store.data) return
  try {
    await navigator.clipboard.writeText(JSON.stringify(store.data, null, indent))
  } catch { /* clipboard denied */ }
}
</script>

<template>
  <div class="debug-shell">
    <!-- Top bar -->
    <header class="debug-header">
      <h1>Elysia Debug</h1>
      <nav class="tab-bar">
        <button
          v-for="t in tabs" :key="t.key"
          :class="['tab-btn', { active: store.activeTab === t.key }]"
          @click="switchTab(t.key)"
        >{{ t.label }}</button>
      </nav>
      <div class="header-actions">
        <button class="action-btn" @click="togglePause">
          {{ store.paused ? '▶ 恢复' : '⏸ 暂停' }}
        </button>
        <button class="action-btn" @click="copyJson(0)">📋 JSON</button>
        <button class="action-btn" @click="copyJson(2)">📋 JSON (light)</button>
      </div>
      <span v-if="store.error" class="poll-error">⚠ {{ store.error }}</span>
      <span v-if="store.paused" class="pause-indicator">已暂停</span>
    </header>

    <!-- 凭据闸门：后端 401 时盖住整个面板——避免「全 0」被误读成真实状态 -->
    <TokenGate
      v-if="authRejected"
      @submit="onToken"
      @clear="onClearToken"
    />

    <!-- Content -->
    <main class="debug-content">
      <component :is="activeComp" />
    </main>
  </div>
</template>

<style>
/* 全局 style — 覆盖 body，不影响主应用 */
:root {
  --debug-bg: #0f0f1a;
  --debug-surface: #1a1a2e;
  --debug-border: #2a2a4a;
  --debug-text: #ddd;
  --debug-muted: #888;
  --debug-accent: #e94560;
  --debug-green: #4caf50;
  --debug-warn: #ff9800;
  --debug-red: #f44336;
}

body {
  font-family: 'Segoe UI', system-ui, monospace;
  background: var(--debug-bg);
  color: var(--debug-text);
  padding: 20px;
  max-width: 1100px;
  margin: 0 auto;
  font-size: 13px;
  line-height: 1.5;
  overflow: auto;
}
</style>

<style scoped>
.debug-shell { display: flex; flex-direction: column; gap: 12px; }

.debug-header {
  display: flex; align-items: center; flex-wrap: wrap; gap: 12px;
  padding: 8px 0; border-bottom: 1px solid var(--debug-border);
}

.debug-header h1 { font-size: 18px; margin: 0; white-space: nowrap; }

.tab-bar { display: flex; gap: 4px; }

.tab-btn {
  padding: 6px 14px; border: 1px solid var(--debug-border); border-radius: 6px;
  background: transparent; color: var(--debug-muted); cursor: pointer;
  font-size: 13px; transition: all 0.2s;
}
.tab-btn:hover { color: var(--debug-text); border-color: var(--debug-accent); }
.tab-btn.active { background: var(--debug-accent); color: #fff; border-color: var(--debug-accent); }

.header-actions { display: flex; gap: 6px; margin-left: auto; }

.action-btn {
  padding: 4px 10px; border: 1px solid var(--debug-border); border-radius: 4px;
  background: transparent; color: var(--debug-muted); cursor: pointer;
  font-size: 12px; transition: all 0.2s;
}
.action-btn:hover { color: var(--debug-text); border-color: var(--debug-accent); }

.poll-error { color: var(--debug-red); font-size: 12px; }
.pause-indicator { color: var(--debug-warn); font-size: 12px; font-weight: 600; }

.debug-content { min-height: 400px; }
</style>
