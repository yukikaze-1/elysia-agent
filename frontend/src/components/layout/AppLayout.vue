<script setup lang="ts">
import CharacterPanel from '@/components/character/CharacterPanel.vue'
import MessageList from '@/components/chat/MessageList.vue'
import PreviewBar from '@/components/input/PreviewBar.vue'
import InputBar from '@/components/input/InputBar.vue'
import Lightbox from '@/components/shared/Lightbox.vue'
import SettingsPanel from '@/components/panels/SettingsPanel.vue'
import ToolsPanel from '@/components/panels/ToolsPanel.vue'
import ApprovalDialog from '@/components/ui/ApprovalDialog.vue'
</script>

<template>
  <SettingsPanel />
  <ToolsPanel />
  <ApprovalDialog />
  <aside id="char-panel">
    <CharacterPanel />
  </aside>
  <main id="main">
    <div id="chat-wrap">
      <MessageList />
    </div>
    <PreviewBar />
    <div id="input-bar">
      <InputBar />
    </div>
    <Lightbox />
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
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 20px 16px;
}

#main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

#chat-wrap {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

#input-bar {
  padding: 10px 16px;
  background: var(--surface);
  border-top: 1px solid var(--border);
  display: flex;
  gap: 8px;
  align-items: flex-end;
}

/* 响应式：窄屏折叠角色面板 */
@media (max-width: 600px) {
  #char-panel {
    width: 60px;
    padding: 16px 8px;
    min-width: 0;
  }
}
</style>
