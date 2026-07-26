<script setup lang="ts">
import { watch, ref, onMounted, nextTick } from 'vue'
import { useChatStore } from '@/stores/chatStore'
import type { ChatMessage as ChatMessageType } from '@/stores/chatStore'
import ChatMessageComp from './ChatMessage.vue'
import TimeDivider from './TimeDivider.vue'
import TypingIndicator from './TypingIndicator.vue'

const chatStore = useChatStore()
const chatWrapRef = ref<HTMLElement | null>(null)
const isLoadingMore = ref(false)

const FIVE_MINUTES = 5 * 60 * 1000
const SCROLL_THRESHOLD = 60

function shouldShowTimeDivider(msg: ChatMessageType, index: number): boolean {
  if (index === 0) return false
  const prev = chatStore.messages[index - 1]
  if (!prev) return false
  return (msg.timestamp - prev.timestamp) >= FIVE_MINUTES
}

function isUserScrolledUp(): boolean {
  const el = chatWrapRef.value
  if (!el) return false
  return el.scrollHeight - el.scrollTop - el.clientHeight > SCROLL_THRESHOLD
}

function scrollToBottom() {
  const el = chatWrapRef.value
  if (!el) return
  el.scrollTop = el.scrollHeight
}

function onScroll() {
  chatStore.userScrolledUp = isUserScrolledUp()
  const el = chatWrapRef.value
  if (el && el.scrollTop <= 80 && !isLoadingMore.value && chatStore.hasMoreMessages) {
    isLoadingMore.value = true
    // fetch loaded — hook for future implementation
    isLoadingMore.value = false
  }
}

// Auto-scroll on new messages
watch(
  () => chatStore.messages.length,
  () => {
    if (!chatStore.userScrolledUp) {
      nextTick(() => scrollToBottom())
    }
  }
)

// Auto-scroll on typing indicator
watch(
  () => chatStore.typing,
  (typing) => {
    if (typing && !chatStore.userScrolledUp) {
      nextTick(() => scrollToBottom())
    }
  }
)

onMounted(() => {
  scrollToBottom()
})
</script>

<template>
  <div
    ref="chatWrapRef"
    class="chat-wrap"
    @scroll="onScroll"
  >
    <TransitionGroup name="bubble">
      <template
        v-for="(msg, i) in chatStore.visibleMessages"
        :key="msg.id"
      >
        <TimeDivider
          v-if="shouldShowTimeDivider(msg, i)"
          :time="msg.timestamp"
        />
        <ChatMessageComp :message="msg" />
      </template>
    </TransitionGroup>
    <TypingIndicator v-if="chatStore.typing" />
  </div>
</template>

<style scoped>
.chat-wrap {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  scroll-behavior: smooth;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.bubble-enter-active {
  transition: opacity 0.3s ease, transform 0.3s ease;
}

.bubble-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
</style>
