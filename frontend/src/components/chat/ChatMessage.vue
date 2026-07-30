<script setup lang="ts">
import { ref } from 'vue'
import type { ChatMessage } from '@/stores/chatStore'
import { useChatStore } from '@/stores/chatStore'
import { useLightbox } from '@/composables/useLightbox'
import ChatBubble from './ChatBubble.vue'
import ToolCard from './ToolCard.vue'
import InnerVoice from './InnerVoice.vue'
import TokenLabel from './TokenLabel.vue'

const props = defineProps<{ message: ChatMessage }>()
const chatStore = useChatStore()
const { open: openLightbox } = useLightbox()

const copyState = ref<'idle' | 'copied'>('idle')
let copyTimer: ReturnType<typeof setTimeout> | null = null

function isPlaying(): boolean {
  return props.message.id === chatStore.playingMessageId
}

function contentText(): string {
  if (typeof props.message.content === 'string') return props.message.content
  // ContentPart[] — extract text
  const textParts = props.message.content.filter((p: any) =>
    p.content && (!p.type || p.type === 'text')
  )
  return textParts.map((p: any) => p.content).join('')
}

/** 从 ContentPart[] 中提取图片 */
function imageParts(): any[] {
  if (typeof props.message.content === 'string') return []
  return (props.message.content as any[]).filter((p: any) =>
    p.type === 'image' && p.src
  )
}

async function copyText() {
  try {
    await navigator.clipboard.writeText(contentText().trim())
    copyState.value = 'copied'
    if (copyTimer) clearTimeout(copyTimer)
    copyTimer = setTimeout(() => { copyState.value = 'idle' }, 2000)
  } catch { /* clipboard denied */ }
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

  <!-- System role (status messages, etc.) — skip if only innerVoice -->
  <template v-else-if="message.role === 'system' && contentText()">
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
        <ChatBubble
          :content="contentText()"
          :contentType="message.contentType"
          :language="message.language"
        />
        <!-- 图片渲染 -->
        <div v-if="imageParts().length" class="msg-images">
          <img
            v-for="(img, i) in imageParts()"
            :key="i"
            class="msg-image"
            :src="img.src"
            loading="lazy"
            @click.stop="openLightbox(img.src)"
            title="点击放大"
          />
        </div>
        <TokenLabel
          v-if="message.tokenUsage"
          :tokenUsage="message.tokenUsage"
        />
        <!-- 复制按钮 — 仅 assistant 气泡，hover 时显示 -->
        <span
          v-if="message.role === 'assistant'"
          class="copy-btn"
          :class="{ copied: copyState === 'copied' }"
          @click.stop="copyText"
        >{{ copyState === 'copied' ? '已复制' : '复制' }}</span>
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

/* ---- Copy button ---- */
.copy-btn {
  display: none;
  position: absolute;
  top: 6px;
  right: 6px;
  background: rgba(255, 255, 255, 0.12);
  border: 1px solid rgba(255, 255, 255, 0.15);
  border-radius: 6px;
  color: #aaa;
  padding: 2px 8px;
  font-size: 0.75rem;
  cursor: pointer;
  backdrop-filter: blur(4px);
  transition: all 0.15s;
}
.copy-btn:hover {
  background: rgba(255, 255, 255, 0.25);
  color: #fff;
}
.copy-btn.copied {
  color: #4ade80;
  border-color: #4ade80;
}
.msg-bubble:hover .copy-btn {
  display: block;
}

/* ---- Images ---- */
.msg-images {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
}
.msg-image {
  max-width: 200px;
  max-height: 200px;
  border-radius: 8px;
  cursor: pointer;
  object-fit: cover;
  border: 1px solid var(--border);
  transition: transform 0.15s;
}
.msg-image:hover {
  transform: scale(1.03);
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

<style>
@import '@/styles/message-layout.css';
</style>
