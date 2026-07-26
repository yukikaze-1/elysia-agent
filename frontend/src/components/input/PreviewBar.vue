<script setup lang="ts">
import { useChatStore } from '@/stores/chatStore'
const chat = useChatStore()
function removePreview(url: string) {
  chat.previewImages = chat.previewImages.filter(p => p.url !== url)
}
</script>

<template>
  <div id="preview-bar" v-if="chat.previewImages.length > 0">
    <div v-for="img in chat.previewImages" :key="img.url" class="preview-item">
      <img :src="img.url" alt="预览" />
      <span class="preview-remove" @click="removePreview(img.url)">×</span>
    </div>
  </div>
</template>

<style scoped>
#preview-bar { display: flex; gap: 8px; padding: 8px; background: var(--surface); border-radius: var(--radius-sm); margin-bottom: 4px; min-height: 48px; align-items: center; flex-wrap: wrap; }
.preview-item { position: relative; width: 56px; height: 56px; border-radius: 6px; overflow: hidden; flex-shrink: 0; border: 1px solid var(--border); }
.preview-item img { width: 100%; height: 100%; object-fit: cover; }
.preview-remove { position: absolute; top: 2px; right: 2px; width: 16px; height: 16px; border-radius: 50%; background: rgba(0,0,0,0.65); color: #fff; font-size: 10px; line-height: 16px; text-align: center; cursor: pointer; display: none; }
.preview-item:hover .preview-remove { display: block; }
.preview-remove:hover { background: var(--accent); }
</style>
