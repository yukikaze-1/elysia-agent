<script setup lang="ts">
import { ref, computed } from 'vue'
import hljs from 'highlight.js'

const props = defineProps<{
  code: string
  language?: string
}>()

const copied = ref(false)

const highlighted = computed(() => {
  const lang = props.language
  if (lang && hljs.getLanguage(lang)) {
    return hljs.highlight(props.code, { language: lang }).value
  }
  return hljs.highlightAuto(props.code).value
})

// Local escape function named _escape to avoid conflict with imported escape utility
function _escape(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

async function copyCode() {
  try {
    await navigator.clipboard.writeText(props.code)
  } catch {
    // Fallback for environments without clipboard API
    const textarea = document.createElement('textarea')
    textarea.value = props.code
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    document.body.removeChild(textarea)
  }
  copied.value = true
  setTimeout(() => { copied.value = false }, 2000)
}
</script>

<template>
  <div class="code-block-wrapper">
    <div class="code-block-header">
      <span class="code-lang" v-html="_escape(language || 'code')"></span>
      <button class="copy-btn" @click="copyCode">
        {{ copied ? '已复制' : '复制' }}
      </button>
    </div>
    <div class="code-block-body">
      <pre><code v-html="highlighted"></code></pre>
    </div>
  </div>
</template>

<style scoped>
.code-block-wrapper {
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid #30363d;
  margin: 6px 0;
}

.code-block-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 12px;
  background: #161b22;
  border-bottom: 1px solid #30363d;
}

.code-lang {
  font-size: 12px;
  color: #8b949e;
  font-family: ui-monospace, Consolas, monospace;
}

.copy-btn {
  font-size: 12px;
  padding: 2px 8px;
  border: 1px solid #30363d;
  border-radius: 4px;
  background: transparent;
  color: #8b949e;
  cursor: pointer;
  transition: color 0.2s, border-color 0.2s;
}

.copy-btn:hover {
  color: #c9d1d9;
  border-color: #8b949e;
}

.code-block-body {
  background: #0d1117;
  overflow-x: auto;
}

.code-block-body pre {
  margin: 0;
  padding: 12px;
}

.code-block-body code {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 13px;
  line-height: 1.5;
  color: #c9d1d9;
}
</style>
