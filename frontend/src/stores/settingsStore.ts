import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import type { CustomProvider } from '@/types/protocol'
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

  const ttsEnabled = ref(localStorage.getItem('elysia_tts') !== 'off')
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

  // ---- Watchers: sync toggles back to localStorage ----

  watch(ttsEnabled, (val) => {
    localStorage.setItem('elysia_tts', val ? 'on' : 'off')
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
   * Update capability flags from the server's capabilities message.
   * Called once on initial connection; keeps all capability fields in one place.
   *
   * 服务端是运行模型的权威来源：把三个服务的当前模型回填到下拉选中值，
   * 覆盖空值或过期的 localStorage 选择——否则首次访问（无 localStorage）
   * 设置面板的三个模型下拉框显示"选择模型..."占位符，即使服务端已在运行某个模型。
   */
  function updateCapabilities(msg: {
    stt?: boolean
    tts?: boolean
    llm_model?: string
    stt_model?: string
    tts_model?: string
    live2d_static_image?: string
  }): void {
    sttAvailable.value = msg.stt !== false
    ttsAvailable.value = msg.tts !== false
    if (msg.llm_model) llmModel.value = msg.llm_model
    if (msg.stt_model) sttModel.value = msg.stt_model
    if (msg.tts_model) ttsModel.value = msg.tts_model
    llmModelName.value = msg.llm_model || ''
    live2dStaticImage.value = msg.live2d_static_image || ''
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
    updateCapabilities,
    switchModel,
  }
})
