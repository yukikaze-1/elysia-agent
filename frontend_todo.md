# 前端代码审查 — TODO

审查日期：2026-07-30
审查范围：`frontend/src/` 全部 51 个源文件

---

## 一、关键问题（High Severity）

### 1. XSS 漏洞 — ChatBubble.vue 未消毒的 `v-html`

[ChatBubble.vue](frontend/src/components/chat/ChatBubble.vue) 使用 `marked.parse()` 将 LLM 输出的 markdown 渲染为 HTML，然后通过 `v-html` 直接注入 DOM。没有任何 HTML 消毒（如 DOMPurify）。攻击者可以通过 prompt injection 让 LLM 输出恶意 `<script>` 标签或其他 XSS payload。

```vue
<!-- ChatBubble.vue -->
<span v-html="renderedHtml"></span>
```

**修复方案：** 使用 DOMPurify 对 `marked.parse()` 的输出进行消毒。

---

### 2. WebSocket 消息处理完全失去类型安全 — useWebSocket.ts

`useWebSocket.ts:25` 使用 `m as any` 强制将服务端消息转为 `any`，导致整个 `ServerMessage` 联合类型判别完全失效。新增消息类型时编译器不会提供任何 exhaustiveness 检查。

```typescript
const m = msg as any  // 抹掉所有类型信息
```

**修复方案：** 使用 `msg as ServerMessage` + 类型收窄（type narrowing）。

---

### 3. 断线重连退避算法被 resetConnection() 破坏 — connectionStore.ts

`connectionStore.ts:40-52` 中 `scheduleReconnect()` 自增 `retryCount`，但定时器回调中调用的 `resetConnection()` 将 `retryCount` 归零。等到真正的 `connect()` 被调用时，`backoff()` 始终以 `retryCount === 0` 计算，导致每次重连等待始终是 1 秒，永远不指数退避。

```typescript
function scheduleReconnect() {
  retryCount.value++           // retryCount = 1
  _reconnectTimer = setTimeout(async () => {
    await resetConnection()    // retryCount = 0 ❌
    connect(_lastUrl)          // backoff(0) = 1000ms
  }, backoff(retryCount.value))
}
```

**修复方案：** 将 `retryCount` 的归零移到成功连接后的回调中。

---

### 4. Debug 轮询定时器泄漏 — debugStore.ts

`debugStore.ts:38-42` 每次切回 Overview 标签页都创建新的 `setInterval`，但不清理旧的。多次切回后产生多个重叠的定时器，轮询速率成倍增长。

```typescript
function startPoll() {
  if (paused.value) return
  fetchDebugData()
  pollTimer = setInterval(fetchDebugData, pollInterval)  // 从不 clearInterval！
}
```

**修复方案：** 在赋值前调用 `clearInterval(pollTimer)`。

---

### 5. SSE 事件 JSON 解析无 try/catch — TasksTab.vue

`TasksTab.vue` 中所有 SSE 事件处理器直接使用 `JSON.parse(data)` 而不包 try/catch，服务端发送畸形数据时将静默崩溃且后续同类型事件全部丢失。

```typescript
// 多个地方类似
const d = JSON.parse(e.data)  // 可能抛出
```

**修复方案：** 所有 `JSON.parse` 包 try/catch。

---

### 6. DAG 可视化永不更新 — TaskCard.vue

`TaskCard.vue` 的 `renderDag()` 仅在 `onMounted` 调用一次。当任务状态在后续 SSE 事件中更新时（例如节点从 executing 变为 completed），vis-network 图表始终保持初始状态。

**修复方案：** 添加 `watch` 监听任务变化并更新 DataSet。

---

### 7. Lightbox Escape 处理完全失效 — App.vue

`App.vue:22-23` 的 Escape 键盘处理器逻辑完全错误：它查找 `.lightbox-hidden` 的元素（即已隐藏的元素），找到后再次添加该 class（无操作），然后 return。`lightboxImage` ref 永远不会被设为 `null`。

```typescript
const el = document.querySelector('.lightbox-hidden')
if (el) { (el as HTMLElement).classList.add('lightbox-hidden'); return }
```

**修复方案：** 调用 `useLightbox().close()` 而不是 DOM 操作。

---

### 8. 麦克风使用已废弃的 ScriptProcessorNode — InputBar.vue

`InputBar.vue` 使用 `audioCtx.createScriptProcessor()` 处理麦克风音频，此 API 已被 Web Audio 标准废弃，未来可能从浏览器中移除。

**修复方案：** 迁移至 `AudioWorklet`。

---

### 9. 音频缓冲区共享导致数据竞争 — InputBar.vue

`connectionStore.sendBinary(int16.buffer)` 传递的是 `Int16Array` 共享的底层 `ArrayBuffer`。如果 WebSocket 发送是异步的（缓冲中），下一次音频回调可能在发送完成前覆写该缓冲区。

**修复方案：** 发送前复制缓冲区 `new Uint8Array(int16.buffer)`。

---

## 二、中等严重度问题（Medium Severity）

| # | 问题 | 文件 | 说明 |
|---|------|------|------|
| 1 | chatStore ↔ audioStore 循环依赖 | `chatStore.ts:4` / `audioStore.ts:123` | 相互导入，依赖 Pinia 延迟初始化解决，但依赖图脆弱 |
| 2 | orphaned 音频块内存泄漏 | `audioStore.ts:25-29` | `audioChunks` 在流从未被 finalize（如 WS 断开）或 TTS 关闭时永不清理 |
| 3 | `onclose` 在新连接建立后仍触发重连 | `connectionStore.ts:99` | 旧 socket 的 `onclose` 无条件调用 `scheduleReconnect()`，导致不必要的重连 |
| 4 | loadMoreMessages 无并发保护 | `chatStore.ts:247-255` | 快速滚动可触发多个并发请求，返回相同批次数据 |
| 5 | LTM/Archive 搜索无防抖 | `LtmPanel.vue` / `ArchivePanel.vue` | 每次按键都触发 API 请求，无 race condition 保护 |
| 6 | 全局 paste 监听器 | `InputBar.vue` | `document` 上的 paste 事件不分来源，在页面任何位置粘贴图片都会触发上传 |
| 7 | VersionHistory 切换 contentType 时数据不刷新 | `VersionHistory.vue` | watch 只监听 `visible`，不监听 `contentType` |
| 8 | PromptViewer/ResponseViewer 双重请求 | `PromptViewer.vue` / `ResponseViewer.vue` | `onMounted` + `immediate: true watch` 导致组件挂载时调用两次 `fetch` |
| 9 | document.body.innerHTML 摧毁 Vue 应用 | `SettingsPanel.vue` | `handleRestart()`/`handleShutdown()` 用 innerHTML 替换整个 body |
| 10 | Lightbox.vue 非响应式 DOM 操作反模式 | `shared/Lightbox.vue` | 使用 `classList.add/remove` 而不是 Vue 的 `v-if`/`v-show` |
| 11 | 断线后手动重连被 onclose 异步覆盖 | `connectionStore.ts` | `disconnect()` 后残留的 `onclose` 仍会触发 `scheduleReconnect()` |
| 12 | `send()` 返回值被所有调用者忽略 | `settingsStore.ts:96` | `switchModel` 不检查 WebSocket 是否真的发出去了 |
| 13 | debugStore 轮询间隔在恢复后永不重置 | `debugStore.ts` | `pollInterval` 变量重置了但运行的定时器不会变，需调用 `restartPollTimer()` |

---

## 三、低严重度问题（Low Severity）

| # | 问题 | 涉及文件 |
|---|------|----------|
| 1 | 图片上传逻辑重复 | `InputBar.vue` 和 `MessageList.vue` 存在相同的 `uploadImage()`/`onDrop()` |
| 2 | `fmt()` 数字格式化函数重复 | `CharStatus.vue` 和 `TokenLabel.vue` |
| 3 | 消息行 CSS 重复 | `TypingIndicator.vue` 复制了 `ChatMessage.vue` 的 `msg-row`/`msg-avatar`/`msg-bubble` |
| 4 | 硬编码中文 locale | `TimeDivider.vue` 使用 `zh-CN`，角色名 "Elysia" 硬编码 |
| 5 | 直接 store 属性赋值 vs `$patch` | `PreviewBar.vue` 直接 `chat.previewImages = ...`，与其他组件不一致 |
| 6 | 无 AbortController | 多个组件中的 fetch 请求无法取消 |
| 7 | `doneTasks` 数组无限增长 | `TasksTab.vue` 已完成任务列表无大小限制 |
| 8 | ConfirmModal 缺 Escape/焦点陷阱 | `shared/ConfirmModal.vue` |
| 9 | ModelSelector 只加载一次 provider 列表 | 运行期间不刷新，必须刷新页面才能看到新模型 |
| 10 | ToggleSwitch 无键盘可访问性 | `ui/ToggleSwitch.vue` 缺乏 `role="switch"` / `aria-checked` / 键盘事件 |
| 11 | TokenLabel 展开用 div 替代 button | `chat/TokenLabel.vue` 缺乏键盘可访问性 |
| 12 | ImageLightbox 缺加载状态/错误处理 | `modals/ImageLightbox.vue` |
| 13 | ArchivePanel 分页参数命名误导 | `offset` vs API 的 `skip` |
| 14 | 角色颜色映射不全 | `SessionPanel.vue` / `ArchivePanel.vue` 的 `roleColor()` 缺少 `system` 等角色 |
| 15 | 硬编码颜色绕过 CSS 变量 | `variables.css:33` 中 `#chat-wrap.drag-over` 使用 `rgba(233,69,96,0.05)` |
| 16 | ExpandableText Unicode 补充平面字符截断 | `shared/ExpandableText.vue` 使用 `slice` 可能切分 2 个 code unit 的字符 |
| 17 | ToolsPanel 按钮与 SettingsPanel 位置重叠 | `panels/ToolsPanel.vue` / `panels/SettingsPanel.vue` 按钮同在 `right: 16px` |

---

## 四、架构与可维护性建议

### 4.1 提取共享 composable

图片上传逻辑应从 `InputBar.vue` / `MessageList.vue` 提取为 `useImageUpload` composable。两者存在完全相同的 `uploadImage()`、`onDrop()`、`onDragOver()` 实现。

### 4.2 迁移至 AudioWorklet

麦克风采集从 `ScriptProcessorNode` 迁移至 `AudioWorklet`。`ScriptProcessorNode` 已被 W3C 标记为废弃。

### 4.3 建立统一的消息分发层

目前 WebSocket 消息流向是：

```
connectionStore.onmessage → _onMessage（unknown 类型）
→ useWebSocket.ts 的 handleMessage（switch on type）
→ 各 store 方法（appendDelta, setMood, setState 等）
```

建议建立明确的 `messageRouter`，将 `ServerMessage.type` 到 store action 的映射集中管理，并添加 try/catch 边界。

### 4.4 提取共享 CSS

Debug 面板的 `LtmPanel.vue`、`SessionPanel.vue`、`ArchivePanel.vue` 之间存在大量重复样式（`.panel`、`.toolbar`、`.field`、`.btn`、`.btn-sm` 等）。可提取为共享 CSS 文件或组件。

### 4.5 建立持久化管理模式

`settingsStore.ts` 中对 localStorage 的 5 组 watch 模式完全相同：

```typescript
watch(property, (val) => {
  localStorage.setItem('elysia_<key>', String(val))
})
```

可提取为通用 `useLocalStorage` composable，或使用第三方库 `pinia-plugin-persist`。

### 4.6 添加 Vue 错误边界

目前没有任何 `errorCaptured` 钩子或全局 `app.config.errorHandler` 配置。渲染过程中的任何未捕获错误会导致整个应用白屏。建议至少添加一个根级错误边界和一个全局 errorHandler。

### 4.7 统一 Store 状态突变模式

部分代码通过 `$patch` 批量更新状态（`chatStore.$patch({ messages: [...] })`），部分代码直接赋值（`chat.previewImages = ...`）。应统一为一种模式，推荐使用 Pinia 的 action。

### 4.8 添加请求取消机制

所有 fetch 请求（Debug 面板的轮询、LTM 查询、消息加载等）均未使用 `AbortController`。组件卸载后，网络回调仍会尝试更新已销毁的响应式状态。建议为所有长时间运行的请求添加取消支持。

---

## 优先级建议

| 批次 | 范围 | 预计工作量 |
|------|------|-----------|
| **P0**（安全 + 功能性 bug） | #1, #3, #4, #5, #7 | 半天 |
| **P1**（类型安全 + 资源泄漏 + 性能） | #2, #6, #8, #9, M-#1~M-#5 | 1 天 |
| **P2**（UX + 健壮性） | M-#6~M-#13 | 1 天 |
| **P3**（代码质量 + 可维护性） | 第三部分 + 架构建议 | 2 天 |
