<script setup lang="ts">
import { useSettingsStore } from '@/stores/settingsStore'
const settings = useSettingsStore()
</script>

<template>
  <div id="avatar-wrap">
    <iframe v-if="settings.live2dEnabled" src="/static/live2d-demo/index.html"
            sandbox="allow-scripts allow-same-origin" id="live2d-frame" />
    <img v-else :src="settings.live2dStaticImage || ''"
         :style="{ display: settings.live2dStaticImage ? '' : 'none' }"
         id="live2d-static-img" />
  </div>
</template>

<style scoped>
#avatar-wrap {
  position: relative; flex: 1; width: 100%; min-height: 0;
  margin-bottom: 12px; overflow: hidden; border-radius: var(--radius);
  background: var(--surface); display: flex; align-items: center; justify-content: center;
}
#avatar-wrap::before { content: '🌸'; font-size: 60px; position: absolute; }
#live2d-frame { width: 100%; height: 100%; border: none; position: relative; z-index: 1; }
#live2d-static-img { width: 100%; height: 100%; object-fit: contain; position: relative; z-index: 1; }
</style>
