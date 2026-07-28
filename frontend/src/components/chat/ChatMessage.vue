<script setup lang="ts">
import type { ChatMessage } from '@/stores/chatStore'
import { useChatStore } from '@/stores/chatStore'
import ChatBubble from './ChatBubble.vue'
import ToolCard from './ToolCard.vue'
import InnerVoice from './InnerVoice.vue'
import TokenLabel from './TokenLabel.vue'

const props = defineProps<{ message: ChatMessage }>()
const chatStore = useChatStore()

function isPlaying(): boolean {
  return props.message.id === chatStore.playingMessageId
}

function contentText(): string {
  if (typeof props.message.content === 'string') return props.message.content
  // 兼容两种格式: {type:"text", content:"..."} 和 {content:"..."}
  const textParts = props.message.content.filter((p: any) =>
    p.content && (!p.type || p.type === 'text')
  )
  return textParts.map((p: any) => p.content).join('')
}

/** 每个句子单独渲染，实现一句一行的分句效果 */
function textSentences(): string[] {
  if (typeof props.message.content === 'string') return [props.message.content]
  return props.message.content
    .filter((p: any) => p.content && (!p.type || p.type === 'text'))
    .map((p: any) => p.content)
}
</script>

<template>
  <!-- InnerVoice bubble — rendered above assistant/user messages, not instead of them -->
  <InnerVoice v-if="message.innerVoice" :content="message.innerVoice" />

  <!-- Tool role: render ToolCard for each toolCall -->
  <template v-if="message.role === 'tool'">
    <div
      v-for="(tc, i) in message.toolCalls"
      :key="tc.id || i"
      class="msg-row tool"
    >
      <ToolCard
        :toolCall="tc"
        :result="message.toolCallResults?.[tc.id]"
      />
    </div>
  </template>

  <!-- System role (status messages, etc.) -->
  <template v-else-if="message.role === 'system'">
    <div class="msg-row system">
      <div class="msg-status">{{ contentText() }}</div>
    </div>
  </template>

  <!-- User / Assistant messages -->
  <template v-else>
    <div
      class="msg-row"
      :class="[
        message.role,
        { playing: isPlaying() }
      ]"
    >
      <div
        v-if="message.role === 'assistant'"
        class="msg-avatar"
      >🌸</div>
      <div
        class="msg-bubble"
        :class="{ playing: isPlaying(), pulse: isPlaying() }"
      >
        <!-- 每个分句独立渲染为一行 -->
        <template v-for="(s, si) in textSentences()" :key="si">
          <span v-if="si > 0"><br /></span>
          <ChatBubble
            :content="s"
            :contentType="message.contentType"
            :language="message.language"
          />
        </template>
        <TokenLabel
          v-if="message.tokenUsage"
          :tokenUsage="message.tokenUsage"
        />
      </div>
      <div
        v-if="message.role === 'user'"
        class="msg-avatar"
      >😎</div>
    </div>
  </template>
</template>

<style scoped>
.msg-row {
  display: flex;
  gap: 10px;
  max-width: 85%;
  animation: fadeIn .2s ease;
}

.msg-row.user {
  align-self: flex-end;
  flex-direction: row-reverse;
}

.msg-row.assistant {
  align-self: flex-start;
}

.msg-row.tool {
  align-self: flex-start;
  max-width: 100%;
}

.msg-row.system {
  align-self: center;
}

.msg-avatar {
  font-size: 28px;
  line-height: 1;
  flex-shrink: 0;
  margin-top: 4px;
}

.msg-bubble {
  padding: 10px 14px;
  border-radius: var(--radius);
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 14px;
  position: relative;
}

.msg-row.assistant .msg-bubble {
  background: var(--assistant-bg);
  border-bottom-left-radius: 4px;
}

.msg-row.user .msg-bubble {
  background: var(--user-bg);
  border-bottom-right-radius: 4px;
}

.msg-bubble.playing {
  box-shadow: 0 0 14px rgba(124, 179, 255, 0.35), inset 0 0 8px rgba(124, 179, 255, 0.08);
  border-color: rgba(124, 179, 255, 0.45);
  transition: box-shadow .3s ease, border-color .3s ease;
}

.msg-bubble.playing.pulse {
  animation: bubble-pulse 1.8s ease-in-out infinite;
}

.msg-status {
  font-size: 12px;
  color: var(--accent);
  font-style: italic;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

@keyframes bubble-pulse {
  0%, 100% { box-shadow: 0 0 10px rgba(124, 179, 255, 0.25), inset 0 0 4px rgba(124, 179, 255, 0.04); }
  50% { box-shadow: 0 0 20px rgba(124, 179, 255, 0.50), inset 0 0 12px rgba(124, 179, 255, 0.12); }
}
</style>
