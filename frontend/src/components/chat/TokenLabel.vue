<script setup lang="ts">
import { ref } from 'vue'
import { fmt } from '@/utils/format'
import type { TokenUsage } from '@/types/protocol'

defineProps<{ tokenUsage: TokenUsage }>()

const expanded = ref(false)

function toggle() {
  expanded.value = !expanded.value
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    toggle()
  }
}
</script>

<template>
  <div
    class="token-label"
    :class="{ expanded }"
    tabindex="0"
    role="button"
    @click="toggle"
    @keydown="handleKeydown"
  >
    <span class="token-summary">&#129409; {{ fmt(tokenUsage.turn_total) }} tokens &#9660;</span>
    <div class="token-detail">
      <div
        v-for="(round, i) in tokenUsage.rounds"
        :key="i"
        class="token-round"
      >
        <span class="token-round-num">#{{ round.round_num }}</span>
        <span class="token-round-item">P:{{ fmt(round.prompt_tokens) }}</span>
        <span class="token-round-item">C:{{ fmt(round.completion_tokens) }}</span>
        <span class="token-round-total">{{ fmt(round.total_tokens) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.token-label {
  margin-top: 6px;
  padding-top: 4px;
  border-top: 1px solid var(--border);
  font-size: 11px;
  color: var(--muted);
  cursor: pointer;
  user-select: none;
}

.token-summary {
  display: inline-block;
}

.token-summary:hover {
  color: var(--text);
}

.token-detail {
  display: none;
  margin-top: 4px;
  padding: 6px 8px;
  background: var(--bg);
  border-radius: 4px;
  font-size: 10px;
  line-height: 1.6;
}

.token-label.expanded .token-detail {
  display: block;
}

.token-label.expanded .token-summary {
  color: var(--accent);
}

.token-round {
  display: flex;
  gap: 8px;
  align-items: baseline;
}

.token-round-num {
  font-weight: 600;
  min-width: 32px;
}

.token-round-item {
  color: var(--muted);
}

.token-round-total {
  font-weight: 600;
  margin-left: auto;
}
</style>
