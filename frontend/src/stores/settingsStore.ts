import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import type { CapabilitiesMessage, CustomProvider } from '@/types/protocol'
import { useConnectionStore } from '@/stores/connectionStore'

/**
 * Persistent settings store backed by localStorage.
 *
 * Toggle-state refs are eagerly initialised from localStorage on store
 * creation and kept in sync via `watch()`. Non-toggle refs
 * (`llmModel`, `sttModel`, `ttsModel`, `customProviders`, capability
 * flags) are written to localStorage explicitly by the caller or by the
 * `switchModel` action.
 *
 * The `switchModel` action persists the selection and sends a
 * `switch_model` message over the WebSocket connection.
 */
export const useSettingsStore = defineStore('settings', () => {
  // ---- Persistent toggles (auto-synced to localStorage) ----

  // ttsEnabled/sttEnabled 现为"后端服务模式"（服务端权威）；localStorage 降级为
  // 建连前的一次性缓存——初始值读取仅用于旧值迁移，capabilities 建连后播种覆盖。
  const ttsEnabled = ref(localStorage.getItem('elysia_tts') !== 'off')
  const sttEnabled = ref(localStorage.getItem('elysia_stt') !== 'off')
  const volume = ref(parseInt(localStorage.getItem('elysia_vol') || '80', 10))
  const wakeEnabled = ref(localStorage.getItem('elysia_wake') === 'on')
  const live2dEnabled = ref(localStorage.getItem('elysia_live2d') === 'on')
  const innerVoiceEnabled = ref(localStorage.getItem('elysia_inner_voice') !== 'off')

  // ---- Persistent model selections (manually saved) ----

  const llmModel = ref(localStorage.getItem('model-llm') ?? '')
  const sttModel = ref(localStorage.getItem('model-stt') ?? '')
  const ttsModel = ref(localStorage.getItem('model-tts') ?? '')

  // ---- Runtime-only state ----

  const customProviders = ref<CustomProvider[]>([])
  const sttAvailable = ref(true)
  const ttsAvailable = ref(true)
  const llmModelName = ref('')
  const live2dStaticImage = ref('')
  const llmDefault = ref('')
  const sttDefault = ref('')
  const ttsDefault = ref('')

  // ---- Watchers: sync toggles back to localStorage（建连前缓存） ----

  watch(ttsEnabled, (val) => {
    localStorage.setItem('elysia_tts', val ? 'on' : 'off')
  })

  watch(sttEnabled, (val) => {
    localStorage.setItem('elysia_stt', val ? 'on' : 'off')
  })

  watch(volume, (val) => {
    localStorage.setItem('elysia_vol', String(val))
  })

  watch(wakeEnabled, (val) => {
    localStorage.setItem('elysia_wake', val ? 'on' : 'off')
    // 同步到后端运行时（audio_buf.set_wake_enabled）
    useConnectionStore().send({ type: 'wake_toggle', enabled: val })
  })

  watch(live2dEnabled, (val) => {
    localStorage.setItem('elysia_live2d', val ? 'on' : 'off')
  })

  watch(innerVoiceEnabled, (val) => {
    localStorage.setItem('elysia_inner_voice', val ? 'on' : 'off')
  })

  // ---- Actions ----

  /**
   * Update capability state from the server's capabilities message.
   * 幂等、且可在任何时刻调用（不仅限于建连）：服务端是运行状态的权威来源，
   * 把嵌套 per-service 的 enabled/available/model/default 全量回填到 store。
   */
  function updateCapabilities(msg: CapabilitiesMessage): void {
    // enabled + available：全量回填（修复扁平形状下 available 缺省时默认 true 的误报）
    sttEnabled.value = msg.services.stt.enabled
    ttsEnabled.value = msg.services.tts.enabled
    sttAvailable.value = msg.services.stt.available
    ttsAvailable.value = msg.services.tts.available
    // default：全量回填
    llmDefault.value = msg.services.llm.default
    sttDefault.value = msg.services.stt.default
    ttsDefault.value = msg.services.tts.default
    // model：非空时回填到下拉选中值（覆盖过期 localStorage 选择；null/空保留既有）
    if (msg.services.llm.model) llmModel.value = msg.services.llm.model
    if (msg.services.stt.model) sttModel.value = msg.services.stt.model
    if (msg.services.tts.model) ttsModel.value = msg.services.tts.model
    llmModelName.value = msg.services.llm.model || ''
    live2dStaticImage.value = msg.live2d_static_image || ''
  }

  /**
   * 设置服务开关（stt/tts 后端模式）。
   * 乐观更新（对齐 switchModel 模式）：立即写 ref（watcher 同步 localStorage），
   * fire-and-forget 发送 set_service，失败由 service_mode_result 回滚、
   * capabilities 重播兜底校正——不等待回包。
   */
  function setService(service: 'stt' | 'tts', enabled: boolean): void {
    if (service === 'stt') sttEnabled.value = enabled
    else ttsEnabled.value = enabled
    useConnectionStore().send({ type: 'set_service', service, enabled })
  }

  /**
   * 旧值迁移：旧版"本机播放"本地开关 → 后端 TTS 模式。
   * 建连后调用一次（幂等）：localStorage 中 elysia_tts=off 表示旧用户曾在本机关闭
   * TTS，迁移为后端模式关闭并同步后端一次（服务端该时刻还不知道此旧偏好）。
   */
  let legacyTtsMigrated = false
  function migrateLegacyTtsMode(): void {
    if (legacyTtsMigrated) return
    legacyTtsMigrated = true
    if (localStorage.getItem('elysia_tts') === 'off') {
      setService('tts', false)
    }
  }

  /**
   * Switch the active model for a given service.
   * Persists the choice to localStorage and sends a `switch_model` message
   * over the WebSocket connection.
   */
  function switchModel(service: 'llm' | 'stt' | 'tts', modelId: string): void {
    const key = `model-${service}`
    localStorage.setItem(key, modelId)

    // Update the corresponding reactive ref so the UI responds immediately
    if (service === 'llm') llmModel.value = modelId
    else if (service === 'stt') sttModel.value = modelId
    else if (service === 'tts') ttsModel.value = modelId

    const connectionStore = useConnectionStore()
    const sent = connectionStore.send({ type: 'switch_model', service, model: modelId })
    if (!sent) {
      // WS 未连接时，模型切换仅持久化到 localStorage，连接恢复后由服务器下发实际模型
      console.warn('switchModel: WS 未连接，模型选择仅保存到本地')
    }
  }

  return {
    ttsEnabled,
    sttEnabled,
    volume,
    wakeEnabled,
    live2dEnabled,
    innerVoiceEnabled,
    llmModel,
    sttModel,
    ttsModel,
    customProviders,
    sttAvailable,
    ttsAvailable,
    llmModelName,
    live2dStaticImage,
    llmDefault,
    sttDefault,
    ttsDefault,
    updateCapabilities,
    setService,
    migrateLegacyTtsMode,
    switchModel,
  }
})
