<script setup lang="ts">
import { ref } from 'vue'
import { useSettingsStore } from '@/stores/settingsStore'
import { useConnectionStore } from '@/stores/connectionStore'
import { useChatStore } from '@/stores/chatStore'
import ToggleSwitch from '@/components/ui/ToggleSwitch.vue'
import ModelSelector from '@/components/ui/ModelSelector.vue'
import type { UnconfiguredEntry } from '@/types/protocol'

const settings = useSettingsStore()
const connection = useConnectionStore()
const chat = useChatStore()

// ---- Panel state ----

const panelOpen = ref(false)

function togglePanel() {
  panelOpen.value = !panelOpen.value
}

function closePanel(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!target.closest('.settings-panel') && !target.closest('.settings-btn')) {
    panelOpen.value = false
  }
}

// ---- Actions ----

function handleClear() {
  if (!confirm('确定要清空所有对话记录吗？此操作不可撤销。')) return
  chat.messages = []
  chat.lastRole = ''
  chat.lastMsgTime = 0
  chat.nextBubbleQueue = []
  chat.playingMessageId = null
  chat.typing = false
}

async function handleRestart() {
  if (!confirm('确定要重启 Elysia 服务吗？')) return
  try {
    await fetch('/restart', { method: 'POST' })
  } catch {
    // fetch 失败说明服务已断开，静默等待恢复
  }
  panelOpen.value = false
  document.body.innerHTML =
    '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--muted);font-size:18px;">服务重启中…</div>'
  // 轮询 /health 等待服务恢复后自动刷新
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 1000))
    try {
      const hr = await fetch('/health')
      if (hr.ok) { location.reload(); return }
    } catch {
      // 继续轮询
    }
  }
  location.reload() // 60 秒超时，强制刷新
}

async function handleShutdown() {
  if (!confirm('确定要关闭 Elysia 服务吗？')) return
  try {
    const res = await fetch('/shutdown', { method: 'POST' })
    if (!res.ok) {
      chat.addStatusMessage('关闭失败')
      return
    }
  } catch {
    // 服务已断开，视为关闭成功
  }
  setTimeout(() => {
    document.body.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--muted);font-size:18px;">Elysia 已关闭 🫧</div>'
  }, 500)
}

function handleModelSwitch(service: 'llm' | 'stt' | 'tts', modelId: string) {
  settings.switchModel(service, modelId)
  // 不乐观插入——等后端 switch_model_result 回包后统一显示（避免成功/失败双插）
}

// TTS 开关现在是后端模式：经 setService 乐观下发，等 service_mode_result/capabilities 回滚校正
function handleTtsToggle(val: boolean) {
  settings.setService('tts', val)
}

// ---- Custom endpoints modal ----

const customModalOpen = ref(false)
const customService = ref('llm')
const customName = ref('')
const customUrl = ref('')
const customKeyEnv = ref('')
const customModels = ref('')
const customTestResult = ref('')
const customTestWaiting = ref(false)

function openCustomModal() {
  customService.value = 'llm'
  customName.value = ''
  customUrl.value = ''
  customKeyEnv.value = ''
  customModels.value = ''
  customTestResult.value = ''
  customModalOpen.value = true
}

async function saveCustomProvider() {
  if (!customName.value.trim() || !customUrl.value.trim()) {
    alert('名称和 Base URL 不能为空')
    return
  }

  const entry = {
    name: customName.value.trim(),
    base_url: customUrl.value.trim(),
    api_key_env: customKeyEnv.value.trim() || undefined,
    models: customModels.value
      ? customModels.value.split(',').map(s => s.trim()).filter(Boolean)
      : [],
  }

  try {
    const res = await fetch('/api/providers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ service: customService.value, entry }),
    })
    const data = await res.json()
    if (data.ok) {
      chat.addStatusMessage(customName.value + ' 已保存')
      customModalOpen.value = false
      location.reload() // 重新加载以更新下拉列表
    } else {
      alert('保存失败: ' + (data.error || '未知错误'))
    }
  } catch (e: any) {
    alert('保存失败: ' + e.message)
  }
}

async function testCustomProvider() {
  if (!customUrl.value.trim()) {
    customTestResult.value = '请输入 Base URL'
    return
  }

  customTestWaiting.value = true
  customTestResult.value = '测试中…'

  try {
    const model = customModels.value
      ? customModels.value.split(',')[0].trim()
      : 'default'
    const res = await fetch('/api/providers/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        service: customService.value,
        base_url: customUrl.value.trim(),
        api_key: '',
        model,
      }),
    })
    const data = await res.json()
    if (data.ok) {
      customTestResult.value = '连接成功 (' + data.latency_ms + 'ms)'
    } else {
      customTestResult.value = data.error || '连接失败'
    }
  } catch (e: any) {
    customTestResult.value = e.message
  } finally {
    customTestWaiting.value = false
  }
}

// ---- Provider 测试 ----
const testResult = ref<Record<'llm' | 'stt' | 'tts', string>>({ llm: '', stt: '', tts: '' })
const testLoading = ref<Record<'llm' | 'stt' | 'tts', boolean>>({ llm: false, stt: false, tts: false })

function currentModelId(service: 'llm' | 'stt' | 'tts'): string {
  if (service === 'llm') return settings.llmModel
  if (service === 'stt') return settings.sttModel
  return settings.ttsModel
}

async function handleTest(service: 'llm' | 'stt' | 'tts', level: 'connectivity' | 'usable') {
  if (testLoading.value[service]) return
  testLoading.value[service] = true
  testResult.value[service] = '测试中…'
  const body: Record<string, string> = { service, level }
  const modelId = currentModelId(service)
  if (modelId && modelId.includes('/')) {
    const [provider, ...rest] = modelId.split('/')
    body.provider = provider
    body.model = rest.join('/')
  }
  // 未选中（disabled 态）→ 不带 provider/model，后端回退配置模型
  try {
    const res = await fetch('/api/providers/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const data = await res.json()
    if (data.ok) {
      const detail = data.detail ? `（${data.detail}）` : ''
      testResult.value[service] = `✓ 连通 (${data.latency_ms}ms)${detail}`
    } else {
      testResult.value[service] = `✗ 失败: ${data.error || '未知错误'}`
    }
  } catch (e: any) {
    testResult.value[service] = `✗ 失败: ${e.message}`
  } finally {
    testLoading.value[service] = false
  }
}

// ---- 未配置引导弹窗 ----
const configModalOpen = ref(false)
const configService = ref<'llm' | 'stt' | 'tts'>('llm')
const configUnconfigured = ref<UnconfiguredEntry[]>([])

function openConfigModal(service: 'llm' | 'stt' | 'tts') {
  configService.value = service
  configModalOpen.value = true
  fetch('/api/providers')
    .then(r => r.json())
    .then((data) => { configUnconfigured.value = data.unconfigured?.[service] ?? [] })
    .catch(() => { configUnconfigured.value = [] })
}

async function copyText(text: string) {
  try { await navigator.clipboard.writeText(text) } catch { /* 忽略 */ }
}
</script>

<template>
  <!-- Settings toggle button -->
  <button
    class="settings-btn"
    @click="togglePanel"
    title="设置"
  >
    ⚙
  </button>

  <!-- Overlay click handler -->
  <div
    v-if="panelOpen"
    class="settings-overlay"
    @click="closePanel"
  ></div>

  <!-- Settings panel -->
  <div
    v-if="panelOpen"
    class="settings-panel"
    @click.stop
  >
    <h3 class="panel-title">设置</h3>

    <!-- Toggles -->
    <div class="setting-row">
      <span class="setting-label">TTS 语音</span>
      <ToggleSwitch
        v-if="settings.ttsAvailable"
        :model-value="settings.ttsEnabled"
        @update:model-value="handleTtsToggle"
      />
      <span
        v-else
        class="unavailable-badge"
        title="TTS 服务不可用"
      >不可用</span>
    </div>

    <div class="setting-row">
      <span class="setting-label">唤醒词</span>
      <ToggleSwitch v-model="settings.wakeEnabled" />
    </div>

    <div class="setting-row">
      <span class="setting-label">Live2D</span>
      <ToggleSwitch v-model="settings.live2dEnabled" />
    </div>

    <div class="setting-row">
      <span class="setting-label">内心独白</span>
      <ToggleSwitch v-model="settings.innerVoiceEnabled" />
    </div>

    <!-- Volume -->
    <div class="setting-row">
      <span class="setting-label">音量</span>
      <div class="vol-control">
        <input
          id="vol-slider"
          v-model.number="settings.volume"
          type="range"
          min="0"
          max="100"
          class="vol-slider"
        />
        <span class="vol-value">{{ settings.volume }}</span>
      </div>
    </div>

    <!-- Model selectors -->
    <div class="model-group">
      <!-- LLM：始终启用 -->
      <div class="setting-row">
        <span class="setting-label">LLM 模型</span>
        <ModelSelector
          service="llm"
          :current-model="settings.llmModel"
          @switch="handleModelSwitch('llm', $event)"
          @open-config="openConfigModal('llm')"
        />
      </div>
      <div class="test-row">
        <button class="panel-action-btn" :disabled="testLoading.llm" @click="handleTest('llm', 'connectivity')">测试连通性</button>
        <button class="panel-action-btn" :disabled="testLoading.llm" @click="handleTest('llm', 'usable')">测试可使用性</button>
        <span class="test-result" :class="{ success: testResult.llm.startsWith('✓'), fail: testResult.llm.startsWith('✗') }">{{ testResult.llm }}</span>
      </div>

      <!-- STT：保留 disabled（未连接禁用下拉） -->
      <div class="setting-row">
        <span class="setting-label">STT 模型<span v-if="!settings.sttAvailable" class="unavailable-badge">（未连接）</span></span>
        <ModelSelector
          service="stt"
          :current-model="settings.sttModel"
          :disabled="!settings.sttAvailable"
          @switch="handleModelSwitch('stt', $event)"
          @open-config="openConfigModal('stt')"
        />
      </div>
      <div class="test-row">
        <button class="panel-action-btn" :disabled="testLoading.stt" @click="handleTest('stt', 'connectivity')">测试连通性</button>
        <button class="panel-action-btn" :disabled="testLoading.stt" @click="handleTest('stt', 'usable')">测试可使用性</button>
        <span class="test-result" :class="{ success: testResult.stt.startsWith('✓'), fail: testResult.stt.startsWith('✗') }">{{ testResult.stt }}</span>
      </div>

      <!-- TTS：保留 disabled（同 STT） -->
      <div class="setting-row">
        <span class="setting-label">TTS 模型<span v-if="!settings.ttsAvailable" class="unavailable-badge">（未连接）</span></span>
        <ModelSelector
          service="tts"
          :current-model="settings.ttsModel"
          :disabled="!settings.ttsAvailable"
          @switch="handleModelSwitch('tts', $event)"
          @open-config="openConfigModal('tts')"
        />
      </div>
      <div class="test-row">
        <button class="panel-action-btn" :disabled="testLoading.tts" @click="handleTest('tts', 'connectivity')">测试连通性</button>
        <button class="panel-action-btn" :disabled="testLoading.tts" @click="handleTest('tts', 'usable')">测试可使用性</button>
        <span class="test-result" :class="{ success: testResult.tts.startsWith('✓'), fail: testResult.tts.startsWith('✗') }">{{ testResult.tts }}</span>
      </div>
    </div>

    <!-- Custom endpoints -->
    <div class="section-divider"></div>
    <button
      class="panel-action-btn"
      @click="openCustomModal"
    >
      + 添加自定义端点
    </button>

    <!-- Action buttons -->
    <div class="section-divider"></div>
    <div class="action-group">
      <button
        class="panel-action-btn danger"
        @click="handleClear"
      >
        清空对话
      </button>
      <button
        class="panel-action-btn"
        @click="handleRestart"
      >
        重启服务
      </button>
      <button
        class="panel-action-btn danger"
        @click="handleShutdown"
      >
        关闭服务
      </button>
    </div>

    <!-- Connection status -->
    <div class="section-divider"></div>
    <div class="conn-status-row">
      <span
        class="conn-dot"
        :class="{ connected: connection.connected }"
      ></span>
      <span class="conn-text">{{ connection.connStatusText }}</span>
    </div>
  </div>

  <!-- Custom endpoints modal -->
  <div
    v-if="customModalOpen"
    class="custom-modal-overlay"
    @click="customModalOpen = false"
  >
    <div
      class="custom-modal"
      @click.stop
    >
      <h4>自定义端点</h4>

      <div class="modal-field">
        <label>服务类型</label>
        <select
          v-model="customService"
          class="model-select"
        >
          <option value="llm">LLM</option>
          <option value="stt">STT</option>
          <option value="tts">TTS</option>
        </select>
      </div>

      <div class="modal-field">
        <label>名称</label>
        <input
          v-model="customName"
          type="text"
          class="modal-input"
          placeholder="例如: my-provider"
        />
      </div>

      <div class="modal-field">
        <label>Base URL</label>
        <input
          v-model="customUrl"
          type="text"
          class="modal-input"
          placeholder="https://api.example.com"
        />
      </div>

      <div class="modal-field">
        <label>API Key 环境变量名</label>
        <input
          v-model="customKeyEnv"
          type="text"
          class="modal-input"
          placeholder="例如: MY_API_KEY"
        />
      </div>

      <div class="modal-field">
        <label>模型列表（逗号分隔）</label>
        <input
          v-model="customModels"
          type="text"
          class="modal-input"
          placeholder="model-a, model-b"
        />
      </div>

      <div class="modal-test-row">
        <button
          class="panel-action-btn"
          :disabled="customTestWaiting"
          @click="testCustomProvider"
        >
          测试连接
        </button>
        <span
          v-if="customTestResult"
          class="test-result"
          :class="{ success: customTestResult.includes('成功'), fail: customTestResult.includes('失败') || customTestWaiting }"
        >
          {{ customTestResult }}
        </span>
      </div>

      <div class="modal-btn-row">
        <button
          class="panel-action-btn"
          @click="customModalOpen = false"
        >
          取消
        </button>
        <button
          class="panel-action-btn primary"
          @click="saveCustomProvider"
        >
          保存
        </button>
      </div>
    </div>
  </div>

  <!-- Unconfigured config modal -->
  <div
    v-if="configModalOpen"
    class="custom-modal-overlay"
    @click="configModalOpen = false"
  >
    <div
      class="custom-modal"
      @click.stop
    >
      <h4>未配置的 {{ { llm: 'LLM', stt: 'STT', tts: 'TTS' }[configService] }} provider</h4>
      <p
        v-if="configUnconfigured.length === 0"
        class="test-result"
      >无——已全部配置。</p>
      <div
        v-for="entry in configUnconfigured"
        :key="entry.name"
        class="modal-field"
      >
        <label>{{ entry.name }}</label>
        <div class="env-copy-row">
          <code class="env-snippet">{{ entry.env_vars.map(v => `export ${v}=xxx`).join('\n') }}</code>
          <button
            class="panel-action-btn"
            @click="copyText(entry.env_vars.map(v => `export ${v}=xxx`).join('\n'))"
          >
            复制
          </button>
        </div>
      </div>
      <p class="test-result">设置环境变量后刷新页面生效。</p>
      <div class="modal-btn-row">
        <button
          class="panel-action-btn"
          @click="configModalOpen = false"
        >
          关闭
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ---- Toggle button ---- */

.settings-btn {
  position: fixed;
  top: 52px;
  right: 16px;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  font-size: 18px;
  cursor: pointer;
  z-index: 1001;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.2s, border-color 0.2s;
}
.settings-btn:hover {
  background: var(--border);
  border-color: var(--accent);
}

/* ---- Overlay ---- */

.settings-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.3);
  z-index: 1001;
}

/* ---- Panel ---- */

.settings-panel {
  position: fixed;
  top: 56px;
  right: 16px;
  width: 300px;
  max-height: calc(100vh - 80px);
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius, 16px);
  padding: 16px;
  z-index: 1002;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.4);
}

.panel-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 12px;
  color: var(--text);
}

/* ---- Setting rows ---- */

.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 0;
  gap: 8px;
}

.setting-label {
  font-size: 13px;
  color: var(--text);
  flex-shrink: 0;
}

.unavailable-badge {
  font-size: 11px;
  color: var(--muted);
  opacity: 0.5;
  cursor: not-allowed;
}

/* ---- Volume ---- */

.vol-control {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  max-width: 160px;
}

.vol-slider {
  flex: 1;
  height: 4px;
  accent-color: var(--accent);
  cursor: pointer;
}

.vol-value {
  font-size: 12px;
  color: var(--muted);
  min-width: 24px;
  text-align: right;
}

/* ---- Model selectors ---- */

.model-group {
  margin-top: 4px;
}

.model-select {
  width: 100%;
  padding: 6px 10px;
  border-radius: var(--radius-sm, 8px);
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  font-size: 12px;
  cursor: pointer;
  outline: none;
  transition: border-color 0.2s;
}
.model-select:focus {
  border-color: var(--accent);
}

/* ---- Section divider ---- */

.section-divider {
  height: 1px;
  background: var(--border);
  margin: 12px 0;
}

/* ---- Action buttons ---- */

.action-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.panel-action-btn {
  width: 100%;
  padding: 8px 12px;
  border-radius: var(--radius-sm, 8px);
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
  transition: background 0.2s, border-color 0.2s;
  text-align: center;
}
.panel-action-btn:hover {
  background: var(--border);
  border-color: var(--accent);
}
.panel-action-btn.danger {
  border-color: #a33;
  color: #f88;
}
.panel-action-btn.danger:hover {
  background: rgba(170, 51, 51, 0.15);
}
.panel-action-btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: white;
}
.panel-action-btn.primary:hover {
  opacity: 0.9;
}
.panel-action-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ---- Connection status ---- */

.conn-status-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}

.conn-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #a33;
  transition: background 0.3s;
}
.conn-dot.connected {
  background: #4caf50;
}

.conn-text {
  font-size: 12px;
  color: var(--muted);
}

/* ---- Custom modal ---- */

.custom-modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1003;
}

.custom-modal {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius, 16px);
  padding: 20px;
  width: 380px;
  max-width: 90vw;
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
}

.custom-modal h4 {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 16px;
  color: var(--text);
}

.modal-field {
  margin-bottom: 12px;
}

.modal-field label {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 4px;
}

.modal-input {
  width: 100%;
  padding: 8px 10px;
  border-radius: var(--radius-sm, 8px);
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  font-size: 13px;
  outline: none;
  transition: border-color 0.2s;
}
.modal-input:focus {
  border-color: var(--accent);
}

.modal-test-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.test-result {
  font-size: 12px;
  color: var(--muted);
}
.test-result.success {
  color: #4caf50;
}
.test-result.fail {
  color: #f88;
}

.modal-btn-row {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

.modal-btn-row .panel-action-btn {
  width: auto;
  min-width: 80px;
}

/* ---- Provider 测试按钮行（下拉兄弟行，flex 内联；.panel-action-btn 默认 width:100%，不覆盖会整行堆叠） ---- */

.test-row {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-top: 4px;
  flex-wrap: wrap;
}
.test-row .panel-action-btn {
  width: auto;
  padding: 4px 10px;
  font-size: 12px;
}

/* ---- 未配置引导弹窗：env 片段 + 复制按钮 ---- */

.env-copy-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.env-copy-row .panel-action-btn {
  width: auto;
  padding: 4px 10px;
  font-size: 12px;
  flex-shrink: 0;
}
.env-snippet {
  flex: 1;
  font-size: 11px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 8px);
  padding: 6px 8px;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text);
}
</style>
