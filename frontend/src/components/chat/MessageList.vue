<script setup lang="ts">
import { watch, ref, onMounted, nextTick } from 'vue'
import { useChatStore } from '@/stores/chatStore'
import { useImageUpload } from '@/composables/useImageUpload'
import type { ChatMessage as ChatMessageType } from '@/stores/chatStore'
import ChatMessageComp from './ChatMessage.vue'
import TimeDivider from './TimeDivider.vue'
import TypingIndicator from './TypingIndicator.vue'

const chatStore = useChatStore()
const chatWrapRef = ref<HTMLElement | null>(null)
const isLoadingMore = ref(false)

const FIVE_MINUTES = 5 * 60 * 1000
const SCROLL_THRESHOLD = 60

// 拖拽上传 — 拖拽到聊天区时在 chat-wrap 上切换 drag-over class
const { onDragOver, onDragLeave, onDrop } = useImageUpload(
  () => chatWrapRef.value
)

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
  requestAnimationFrame(() => { el.scrollTop = el.scrollHeight })
}

async function onScroll() {
  const wasUp = chatStore.userScrolledUp
  chatStore.userScrolledUp = isUserScrolledUp()
  // 用户滚动回底部时自动隐藏指示器
  if (!chatStore.userScrolledUp && wasUp) {
    newMsgVisible.value = false
  }
  const el = chatWrapRef.value
  if (el && el.scrollTop <= 80 && !isLoadingMore.value && chatStore.hasMoreMessages) {
    isLoadingMore.value = true
    const prevScrollHeight = el.scrollHeight
    const loaded = await chatStore.loadMoreMessages()
    if (loaded) {
      // 保持滚动位置：新 scrollHeight - 旧 scrollHeight = 新增内容高度
      await nextTick()
      el.scrollTop = el.scrollHeight - prevScrollHeight
    }
    isLoadingMore.value = false
  }
}

// ---- 新消息浮动指示器 ----
const newMsgVisible = ref(false)

function showNewMsgIndicator() {
  newMsgVisible.value = true
}

function scrollToBottomAndHide() {
  chatStore.userScrolledUp = false
  newMsgVisible.value = false
  scrollToBottom()
}

// Auto-scroll on new messages or content change (streaming delta)
watch(
  () => {
    const msgs = chatStore.messages
    const last = msgs[msgs.length - 1]
    return { count: msgs.length, lastContent: last?.content }
  },
  () => {
    if (!chatStore.userScrolledUp) {
      nextTick(() => scrollToBottom())
    } else {
      showNewMsgIndicator()
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
    @dragover="onDragOver"
    @dragenter="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
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
    <!-- 新消息浮动指示器 -->
    <div
      v-if="newMsgVisible"
      id="new-msg-indicator"
      class="visible"
      @click="scrollToBottomAndHide"
    >↓ 新消息</div>
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

/* 新消息浮动指示器 */
#new-msg-indicator {
  display: none;
  position: fixed;
  bottom: 80px;
  left: 50%;
  transform: translateX(-50%);
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 20px;
  padding: 8px 18px;
  font-size: 13px;
  cursor: pointer;
  z-index: 20;
  white-space: nowrap;
  box-shadow: 0 2px 12px rgba(233, 69, 96, 0.4);
  transition: transform .15s ease;
}
#new-msg-indicator:hover {
  transform: translateX(-50%) scale(1.04);
}
#new-msg-indicator.visible {
  display: block;
}
</style>
