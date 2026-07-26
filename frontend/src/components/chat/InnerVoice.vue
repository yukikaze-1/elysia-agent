<script setup lang="ts">
defineProps<{ content: string }>()
</script>

<template>
  <div class="inner-voice-bubble" v-if="content">{{ content }}</div>
</template>

<style scoped>
.inner-voice-bubble {
  color: #888;
  font-style: italic;
  font-size: 0.85em;
  padding: 4px 12px;
  margin: 2px 0;
  border-left: 2px solid #ccc;
  opacity: 0.8;
  max-width: 80%;
}
</style>
