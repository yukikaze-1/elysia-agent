<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted, inject, type Ref } from 'vue'
import { useConnectionStore } from '@/stores/connectionStore'

interface ApprovalData {
  tool: string
  toolParams?: string
  request_id: string
}

const data = inject<Ref<ApprovalData | null>>('approvalRequest', ref(null))
const visible = ref(false)
const connectionStore = useConnectionStore()

watch(data, (val) => {
  visible.value = val !== null
}, { immediate: true })

function approve() {
  if (!data.value) return
  connectionStore.send({
    type: 'tool_approval',
    request_id: data.value.request_id,
    approved: true,
  })
  visible.value = false
}

function deny() {
  if (!data.value) return
  connectionStore.send({
    type: 'tool_approval',
    request_id: data.value.request_id,
    approved: false,
  })
  visible.value = false
}

function onDocumentKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && visible.value) {
    deny()
  }
}

onMounted(() => document.addEventListener('keydown', onDocumentKeydown))
onUnmounted(() => document.removeEventListener('keydown', onDocumentKeydown))
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div v-if="visible" class="approval-overlay" @click.self="deny">
        <div class="approval-dialog">
          <div class="approval-title">工具调用审批</div>
          <div class="approval-body">
            <div class="approval-row">
              <span class="approval-label">工具</span>
              <span class="approval-value">{{ data?.tool }}</span>
            </div>
            <div v-if="data?.toolParams" class="approval-row">
              <span class="approval-label">参数</span>
              <pre class="approval-value code">{{ data.toolParams }}</pre>
            </div>
          </div>
          <div class="approval-actions">
            <button class="btn btn-deny" @click="deny">拒绝</button>
            <button class="btn btn-approve" @click="approve">允许</button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.approval-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}

.approval-dialog {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 12px);
  padding: 24px;
  min-width: 360px;
  max-width: 480px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
}

.approval-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 16px;
}

.approval-body {
  margin-bottom: 20px;
}

.approval-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 12px;
}

.approval-label {
  font-size: 11px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.approval-value {
  font-size: 14px;
  color: var(--text);
}

.approval-value.code {
  font-family: monospace;
  font-size: 12px;
  background: var(--bg);
  padding: 8px 10px;
  border-radius: 6px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow-y: auto;
}

.approval-actions {
  display: flex;
  gap: 12px;
  justify-content: flex-end;
}

.btn {
  padding: 8px 20px;
  border-radius: 8px;
  border: 1px solid var(--border);
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s;
  font-family: inherit;
}

.btn-deny {
  background: transparent;
  color: var(--muted);
  border-color: var(--border);
}

.btn-deny:hover {
  color: var(--accent);
  border-color: var(--accent);
  background: rgba(233, 69, 96, 0.1);
}

.btn-approve {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}

.btn-approve:hover {
  filter: brightness(1.15);
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
