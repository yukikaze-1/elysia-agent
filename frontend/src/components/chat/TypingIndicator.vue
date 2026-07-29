<template>
  <div class="msg-row assistant">
    <div class="msg-avatar">🌸</div>
    <div class="msg-bubble typing-dots">
      <span></span><span></span><span></span>
    </div>
  </div>
</template>

<!-- 共享消息布局样式（与 ChatMessage.vue 共用） -->
<style>
@import '@/styles/message-layout.css';
</style>

<style scoped>
.typing-dots {
  display: flex;
  gap: 4px;
  padding: 14px 14px;
}

.typing-dots span {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--muted);
  animation: bounce 1.4s infinite;
}

.typing-dots span:nth-child(2) {
  animation-delay: .2s;
}

.typing-dots span:nth-child(3) {
  animation-delay: .4s;
}

@keyframes bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: .4; }
  30% { transform: translateY(-6px); opacity: 1; }
}
</style>
