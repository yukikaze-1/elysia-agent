<template>
  <div class="msg-row assistant">
    <div class="msg-avatar">🌸</div>
    <div class="msg-bubble typing-dots">
      <span></span><span></span><span></span>
    </div>
  </div>
</template>

<style scoped>
.msg-row {
  display: flex;
  gap: 10px;
  max-width: 85%;
  align-self: flex-start;
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
  background: var(--assistant-bg);
  border-bottom-left-radius: 4px;
}

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
