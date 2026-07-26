<script setup lang="ts">
import { computed } from 'vue'
import { useChatStore } from '@/stores/chatStore'
import { useSettingsStore } from '@/stores/settingsStore'

const chat = useChatStore()
const settings = useSettingsStore()

const moodDisplay = computed(() =>
  chat.mood ? `${chat.mood} (${Math.round(chat.moodIntensity * 100)}%)` : ''
)
</script>

<template>
  <div id="char-info">
    <div :class="['state-dot', chat.fsmState]" />
    <div id="char-name">Elysia</div>
  </div>
  <div id="llm-model-tag" v-if="settings.llmModelName">{{ settings.llmModelName }}</div>
  <div id="session-token-bar" />
  <div id="char-state">{{ chat.fsmState }}</div>
  <div id="mood-tag" v-if="moodDisplay">{{ moodDisplay }}</div>
</template>

<style scoped>
#char-info { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.state-dot {
  display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  background: var(--idle); transition: background 0.3s;
}
.state-dot.thinking { background: var(--thinking); }
.state-dot.speaking { background: var(--speaking); }
.state-dot.executing { background: var(--executing); }
#char-name { font-size: 18px; font-weight: 600; margin-bottom: 4px; }
#char-state { font-size: 12px; color: var(--muted); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }
#mood-tag { font-size: 13px; color: var(--muted); text-align: center; min-height: 20px; }
#llm-model-tag {
  font-size: 10px; color: var(--accent); text-align: center; margin-bottom: 4px;
  border: 1px solid var(--border); border-radius: 4px; padding: 1px 6px;
  max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
</style>
