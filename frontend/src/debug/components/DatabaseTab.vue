<script setup lang="ts">
import LtmPanel from './LtmPanel.vue'
import SessionPanel from './SessionPanel.vue'
import ArchivePanel from './ArchivePanel.vue'
</script>

<template>
  <section class="db-tab">
    <LtmPanel />
    <SessionPanel />
    <ArchivePanel />
  </section>
</template>

<style scoped>
.db-tab { display: flex; flex-direction: column; gap: 16px; }
</style>
