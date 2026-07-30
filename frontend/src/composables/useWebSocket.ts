import { onMounted, onUnmounted, type Ref } from 'vue'
import { useConnectionStore } from '@/stores/connectionStore'
import { useChatStore } from '@/stores/chatStore'
import { useAudioStore } from '@/stores/audioStore'
import { useSettingsStore } from '@/stores/settingsStore'
import type {
  ActionMessage, AudioEndMessage, AudioMessage, CapabilitiesMessage, CommandMessage,
  DeltaMessage, ErrorMessage, ExpressionMessage, MessageMessage, ServerMessage,
  SessionMessage, StateMessage, SwitchModelResultMessage, TokenUsageMessage,
  TranscriptionMessage, TypingMessage,
} from '@/types/protocol'

// 转录情绪/事件 emoji 映射（与服务端同步）
const EMOTION_EMOJI: Record<string, string> = {
  HAPPY: '😊', SAD: '😔', ANGRY: '😡', NEUTRAL: '',
  FEARFUL: '😰', DISGUSTED: '🤢', SURPRISED: '😮',
}
const EVENT_EMOJI: Record<string, string> = {
  BGM: '🎼', Applause: '👏', Laughter: '😀', Cry: '😭',
  Cough: '😷', Sneeze: '🤧',
}

export function useWebSocket(approvalRequest?: Ref<{ tool: string; toolParams?: string; request_id: string } | null>) {
  const connection = useConnectionStore()
  const chat = useChatStore()
  const audio = useAudioStore()
  const settings = useSettingsStore()

  function handleMessage(msg: unknown) {
    const m = msg as ServerMessage
    switch (m.type) {
      case 'delta': {
        const d = m as DeltaMessage
        chat.appendDelta(d.content, d.stream_id)
        break
      }
      case 'message':       chat.handleCompleteMessage(m as MessageMessage); break
      case 'audio': {
        const a = m as AudioMessage
        audio.bufferChunk(a.stream_id, a.seq, a.data)
        break
      }
      case 'audio_end': {
        const ae = m as AudioEndMessage
        audio.finalizeStream(ae.stream_id, ae.format)
        break
      }
      case 'action':        handleAction(m as ActionMessage); break
      case 'command':       handleCommand(m as CommandMessage); break
      case 'session': {
        const s = m as SessionMessage
        chat.restoreFromServer(s.messages, s.token_stats)
        break
      }
      case 'capabilities':  settings.updateCapabilities(m as CapabilitiesMessage); break
      case 'typing':        chat.typing = (m as TypingMessage).typing; break
      case 'transcription': {
        const t = m as TranscriptionMessage
        let display = t.text
        const emotionEmoji = EMOTION_EMOJI[t.emotion as string] || ''
        if (emotionEmoji) display += ' ' + emotionEmoji
        if (t.events && t.events.length > 0) {
          for (const evt of t.events) {
            const evtEmoji = EVENT_EMOJI[evt] || ''
            if (evtEmoji) display += ' ' + evtEmoji
          }
        }
        chat.addUserMessage('🎤 ' + display)
        break
      }
      case 'expression': {
        const ex = m as ExpressionMessage
        chat.setMood(ex.expression, ex.intensity)
        break
      }
      case 'state': {
        const st = m as StateMessage
        chat.setState(st.state)
        if (st.state === 'executing') chat.finalizeDelta()
        break
      }
      case 'token_usage':   chat.updateTokenUsage(m as TokenUsageMessage); break
      case 'error':         console.warn('Server:', (m as ErrorMessage).message); break
      case 'wake':          if (settings.wakeEnabled) audio.flashWakeIndicator(); break
      case 'switch_model_result': handleSwitchResult(m as SwitchModelResultMessage); break
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
