<script setup lang="ts">
import { computed } from 'vue'
import { marked } from 'marked'
import CodeBlock from '@/components/shared/CodeBlock.vue'

const props = defineProps<{
  content: string
  contentType?: 'text' | 'markdown' | 'code'
  language?: string
}>()

const renderedHtml = computed(() => {
  if (props.contentType !== 'markdown') return ''
  return marked.parse(props.content) as string
})
</script>

<template>
  <!-- Code block: render CodeBlock component -->
  <CodeBlock
    v-if="contentType === 'code'"
    :code="content"
    :language="language"
  />

  <!-- Markdown: render parsed HTML -->
  <div
    v-else-if="contentType === 'markdown'"
    class="markdown-body"
    v-html="renderedHtml"
  ></div>

  <!-- Plain text: fallback -->
  <span v-else class="msg-text">{{ content }}</span>
</template>

<style scoped>
.msg-text {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 14px;
  line-height: 1.55;
}

.markdown-body {
  padding: 0;
}

.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3) {
  margin: 8px 0 4px;
  font-size: 15px;
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  padding-left: 20px;
  margin: 4px 0;
}

.markdown-body :deep(li) {
  margin: 2px 0;
}

.markdown-body :deep(p) {
  margin: 4px 0;
}

.markdown-body :deep(code) {
  background: rgba(255, 255, 255, 0.1);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 13px;
}

.markdown-body :deep(pre) {
  background: #0d1117;
  border-radius: 6px;
  padding: 10px;
  overflow-x: auto;
  margin: 6px 0;
}

.markdown-body :deep(pre code) {
  background: none;
  padding: 0;
}

.markdown-body :deep(table) {
  border-collapse: collapse;
  margin: 6px 0;
  font-size: 13px;
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid #30363d;
  padding: 4px 8px;
  text-align: left;
}

.markdown-body :deep(th) {
  background: #161b22;
}

.markdown-body :deep(blockquote) {
  border-left: 3px solid #58a6ff;
  padding-left: 10px;
  margin: 6px 0;
  color: #8b949e;
}

.markdown-body :deep(a) {
  color: #58a6ff;
}
</style>
