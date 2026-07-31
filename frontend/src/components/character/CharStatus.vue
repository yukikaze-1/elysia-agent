<script setup lang="ts">
import { computed } from 'vue'
import { useChatStore } from '@/stores/chatStore'
import { useSettingsStore } from '@/stores/settingsStore'
import { fmt } from '@/utils/format'

const chat = useChatStore()
const settings = useSettingsStore()

const moodDisplay = computed(() =>
  chat.mood ? `${chat.mood} (${Math.round(chat.moodIntensity * 100)}%)` : ''
)

const sessionTokenDisplay = computed(() => {
  const st = chat.sessionTokens
  if (!st || !st.total) return ''
  const models = Object.entries(st.by_model || {})
  const parts = models.map(([m, t]) => {
    const short = m.split('/').pop() || m
    return short + ': ' + fmt(t as number)
  })
  return '\u{1FA99} 总计 ' + fmt(st.total) + (parts.length ? ' (' + parts.join(', ') + ')' : '')
})
</script>

<template>
  <div id="char-info">
    <div :class="['state-dot', chat.fsmState]" />
    <div id="char-name">Elysia</div>
  </div>
  <div id="llm-model-tag" v-if="settings.llmModelName">{{ settings.llmModelName }}</div>
  <div id="session-token-bar" v-if="sessionTokenDisplay">{{ sessionTokenDisplay }}</div>
  <div id="char-state">{{ chat.fsmState }}</div>
  <div id="mood-tag" v-if="moodDisplay">{{ moodDisplay }}</div>
</template>

<style scoped>
#char-info { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.state-dot {
  display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  background: var(--idle); transition: background 0.3s; vertical-align: middle;
}
.state-dot.thinking { background: var(--thinking); animation: pulse-dot 1s infinite; }
.state-dot.speaking { background: var(--speaking); }
.state-dot.executing { background: var(--executing); animation: pulse-dot .5s infinite; }
@keyframes pulse-dot { 0%,100%{opacity:1} 50%{opacity:.3} }
#char-name { font-size: 18px; font-weight: 600; margin-bottom: 4px; }
#char-state { font-size: 12px; color: var(--muted); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }
#mood-tag { font-size: 13px; color: var(--muted); text-align: center; min-height: 20px; transition: color .3s; }
#llm-model-tag {
  font-size: 10px; color: var(--accent); text-align: center;
  margin-bottom: 4px; min-height: 14px; letter-spacing: 0.5px;
  border: 1px solid var(--border); border-radius: 4px;
  padding: 1px 6px; max-width: 200px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
#session-token-bar {
  font-size: 11px; color: var(--muted); text-align: center;
  padding: 2px 8px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* 响应式：窄屏隐藏角色信息 */
@media (max-width: 600px) {
  #char-name, #char-state, #mood-tag, #char-info { display: none; }
}
</style>
