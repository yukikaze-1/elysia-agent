<script setup lang="ts">
import { ref } from 'vue'

const lb = ref<HTMLElement | null>(null)
const li = ref<HTMLElement | null>(null)

function open(url: string) {
  if (li.value) li.value.setAttribute('src', url)
  if (lb.value) lb.value.classList.remove('lightbox-hidden')
}

function close() {
  if (lb.value) lb.value.classList.add('lightbox-hidden')
}

defineExpose({ open, close })
</script>

<template>
  <div ref="lb" id="lightbox" class="lightbox-hidden" @click.self="close">
    <img ref="li" id="lightbox-img" />
  </div>
</template>

<style scoped>
#lightbox { position: fixed; inset: 0; z-index: 9999; display: flex; align-items: center; justify-content: center; background: rgba(0,0,0,0.85); cursor: pointer; }
.lightbox-hidden { display: none !important; }
#lightbox-img { max-width: 90vw; max-height: 90vh; object-fit: contain; border-radius: 4px; }
</style>
