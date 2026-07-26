<script setup lang="ts">
</script>

<template>
  <aside id="char-panel">
  </aside>
  <main id="main">
    <div id="chat-wrap">
    </div>
    <div id="input-bar">
    </div>
  </main>
</template>

<style scoped>
#char-panel {
  width: 35vw;
  max-width: 500px;
  min-width: 220px;
  flex-shrink: 0;
  background: var(--surface);
  border-right: 1px solid var(--border);
}

#main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

#chat-wrap {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
}

#input-bar {
  padding: 10px 16px;
  background: var(--surface);
  border-top: 1px solid var(--border);
  display: flex;
  gap: 8px;
  align-items: flex-end;
}
</style>
