import { onMounted, onUnmounted, type Ref } from 'vue'
import { useConnectionStore } from '@/stores/connectionStore'
import { useChatStore } from '@/stores/chatStore'
import { useAudioStore } from '@/stores/audioStore'
import { useSettingsStore } from '@/stores/settingsStore'
import type { ActionMessage, CommandMessage, SwitchModelResultMessage } from '@/types/protocol'

export function useWebSocket(approvalRequest?: Ref<{ tool: string; toolParams?: string; request_id: string } | null>) {
  const connection = useConnectionStore()
  const chat = useChatStore()
  const audio = useAudioStore()
  const settings = useSettingsStore()

  function handleMessage(msg: unknown) {
    const m = msg as any
    switch (m.type) {
      case 'delta':         chat.appendDelta(m.content, m.stream_id); break
      case 'message':       if (chat.handleCompleteMessage(m)) chat.finalizeDelta(); break
      case 'audio':         audio.bufferChunk(m.stream_id, m.seq, m.data); break
      case 'audio_end':     audio.finalizeStream(m.stream_id, m.format); break
      case 'action':        handleAction(m); break
      case 'command':       handleCommand(m); break
      case 'session':       chat.restoreFromServer(m.messages, m.token_stats); break
      case 'capabilities':  settings.sttAvailable = m.stt !== false
                            settings.ttsAvailable = m.tts !== false
                            settings.llmModelName = m.llm_model || ''
                            settings.live2dStaticImage = m.live2d_static_image || ''; break
      case 'typing':        chat.typing = m.typing; break
      case 'transcription': chat.addUserMessage('🎤 ' + m.text); break
      case 'expression':    chat.setMood(m.expression, m.intensity); break
      case 'state':         chat.setState(m.state)
                            if (m.state === 'executing') chat.finalizeDelta(); break
      case 'token_usage':   chat.updateTokenUsage(m); break
      case 'error':         console.warn('Server:', m.message); break
      case 'wake':          if (settings.wakeEnabled) audio.flashWakeIndicator(); break
      case 'switch_model_result': handleSwitchResult(m); break
      default:              console.warn('Unknown type:', m.type); break
    }
  }

  function handleAction(msg: ActionMessage) {
    if (msg.action === 'tool_executing')
      chat.addToolCard(msg.params.tool as string, msg.params.call_id as string, msg.params.args as Record<string, unknown> | undefined)
    else if (msg.action === 'tool_done')
      chat.updateToolCard(msg.params.call_id as string, msg.params as any)
  }

  function handleCommand(msg: CommandMessage) {
    switch (msg.command) {
      case 'tool_approval':
        if (approvalRequest && msg.params) {
          approvalRequest.value = {
            tool: msg.params.tool as string || '',
            toolParams: msg.params.params as string || msg.params.argsJson as string || '',
            request_id: msg.params.request_id as string || '',
          }
        }
        break
      case 'block':         chat.addBlockBubble(msg.params || {}); break
      case 'inner_voice':   chat.addInnerVoice((msg.params?.content as string) || ''); break
    }
  }

  function handleSwitchResult(msg: SwitchModelResultMessage) {
    if (msg.ok) {
      chat.addStatusMessage(msg.service.toUpperCase() + ' 已切换至 ' + msg.model)
      if (msg.service === 'llm') settings.llmModelName = msg.model
    } else {
      chat.addStatusMessage(msg.service.toUpperCase() + ' 切换失败: ' + (msg.error || '未知错误'))
    }
  }

  onMounted(() => {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    connection.connect(`${protocol}//${location.host}/ws`, handleMessage)
  })

  onUnmounted(() => connection.disconnect())
}
