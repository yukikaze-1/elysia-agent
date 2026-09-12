<script setup lang="ts">
/**
 * 管理面凭据闸门（S2-F3）。
 *
 * 只在后端返回 401 时出现——没配 SERVER_AUTH_TOKEN 的后端会直接放行，
 * 回环本地开发零打扰（不预设「一定需要 token」）。
 */
import { ref, onMounted, nextTick } from 'vue'

const emit = defineEmits<{ submit: [token: string]; clear: [] }>()

const value = ref('')
const reveal = ref(false)
const inputRef = ref<HTMLInputElement | null>(null)

onMounted(() => nextTick(() => inputRef.value?.focus()))
</script>

<template>
  <div class="gate-overlay">
    <form class="gate" @submit.prevent="emit('submit', value)">
      <h2>需要管理面凭据</h2>
      <p>
        后端拒绝了本次请求（<code>401</code>）。<code>/debug/api/*</code> 属管理面，
        只认 <code>SERVER_AUTH_TOKEN</code>——产品 token（<code>AGENT_ACCESS_TOKEN</code>）
        不算数。
      </p>
      <p class="hint">
        凭据只存在本标签页的 <code>sessionStorage</code>，随标签页关闭失效；
        只经 <code>Authorization</code> 头发送，<strong>不进 URL、不落日志</strong>。
      </p>
      <input
        ref="inputRef"
        v-model="value"
        :type="reveal ? 'text' : 'password'"
        placeholder="SERVER_AUTH_TOKEN"
        autocomplete="off"
        spellcheck="false"
      />
      <label class="reveal">
        <input v-model="reveal" type="checkbox" /> 显示明文
      </label>
      <div class="actions">
        <button type="submit" :disabled="!value.trim()">保存并重试</button>
        <button type="button" class="ghost" @click="emit('clear')">清除已存凭据</button>
      </div>
    </form>
  </div>
</template>

<style scoped>
.gate-overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 15, 26, 0.92);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.gate {
  width: min(520px, 92vw);
  background: var(--debug-surface);
  border: 1px solid var(--debug-border);
  border-radius: 8px;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
h2 { margin: 0; font-size: 16px; color: var(--debug-accent); }
p { margin: 0; font-size: 13px; color: var(--debug-text); line-height: 1.6; }
.hint { color: var(--debug-muted); font-size: 12px; }
code { background: rgba(255, 255, 255, 0.08); padding: 1px 4px; border-radius: 3px; font-size: 12px; }
input[type='password'], input[type='text'] {
  background: var(--debug-bg);
  border: 1px solid var(--debug-border);
  border-radius: 4px;
  color: var(--debug-text);
  padding: 8px 10px;
  font-family: monospace;
  font-size: 13px;
}
input[type='password']:focus, input[type='text']:focus { outline: 1px solid var(--debug-accent); }
.reveal { font-size: 12px; color: var(--debug-muted); display: flex; align-items: center; gap: 6px; }
.actions { display: flex; gap: 10px; }
button {
  background: var(--debug-accent);
  border: none;
  border-radius: 4px;
  color: #fff;
  padding: 8px 16px;
  font-size: 13px;
  cursor: pointer;
}
button:disabled { opacity: 0.45; cursor: not-allowed; }
button.ghost { background: transparent; border: 1px solid var(--debug-border); color: var(--debug-muted); }
</style>
