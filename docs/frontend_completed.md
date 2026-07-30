# 前端代码审查 — 已完成

审查日期：2026-07-30
修复日期：2026-07-30
提交：`60ac3e5` + `99e821c`（第一轮，34 文件） + `c912a8f`（第二轮，14 文件）

> 本文归档已修复的问题。未修复项见 [frontend_todo.md](../frontend_todo.md)。

---

## 第一轮修复（`60ac3e5` + `99e821c`）

### 一、关键问题（High Severity）— 7/9 已修复

### 1. ✅ XSS 漏洞 — ChatBubble.vue 未消毒的 `v-html`

[ChatBubble.vue](../frontend/src/components/chat/ChatBubble.vue) 使用 `marked.parse()` 将 LLM 输出的 markdown 渲染为 HTML，然后通过 `v-html` 直接注入 DOM。没有任何 HTML 消毒（如 DOMPurify）。攻击者可以通过 prompt injection 让 LLM 输出恶意 `<script>` 标签或其他 XSS payload。

**修复：** `99e821c` — 安装 dompurify + @types/dompurify，`DOMPurify.sanitize(marked.parse(content))`

---

### 2. ✅ WebSocket 消息处理完全失去类型安全 — useWebSocket.ts

`useWebSocket.ts:25` 使用 `m as any` 强制将服务端消息转为 `any`，导致整个 `ServerMessage` 联合类型判别完全失效。

**修复：** `99e821c` — `msg as ServerMessage`，各 case 分支导入对应窄类型

---

### 3. ✅ 断线重连退避算法被 resetConnection() 破坏 — connectionStore.ts

`scheduleReconnect()` 中 `resetConnection()` 将 `retryCount` 归零，导致 `backoff()` 始终以 `retryCount === 0` 计算，每次重连等待始终是 1 秒。

**修复：** `99e821c` — `retryCount++` 移到 `backoff()` 之前；移除 setTimeout 回调中的 `resetConnection()`；成功连接时 `onopen` 已归零

---

### 4. ✅ Debug 轮询定时器泄漏 — debugStore.ts

`startPoll()` 创建 `setInterval` 不先 `clearInterval`，存在潜在泄漏风险。

**修复：** `99e821c` — `startPoll()` 开头增加防御式 `clearInterval(pollTimer)`，与 `restartPollTimer()` 模式统一

---

### 5. ✅ SSE 事件 JSON 解析无 try/catch — TasksTab.vue

所有 5 个 SSE 事件处理器直接使用 `JSON.parse(e.data)` 而不包 try/catch。

**修复：** `99e821c` — 全部 try/catch 包裹，异常时 console.warn 并优雅降级

---

### 6. ✅ DAG 可视化永不更新 — TaskCard.vue

`renderDag()` 仅在 `onMounted` 调用一次，SSE `node_update` 事件无法更新 vis-network 图表。

**修复：** `99e821c` — 提取 `buildNodes()`/`buildEdges()`；添加 deep watch 更新 DataSet（clear + add），不重建 Network 实例

---

### 7. ✅ Lightbox Escape 处理完全失效 — App.vue

Escape 键盘处理器查找 `.lightbox-hidden`（已隐藏元素），找到后再次添加该 class（no-op），从未真正关闭灯箱。

**修复：** `99e821c` — 改用 `useLightbox()` composable，`lightboxImage !== null` 则 `close()` 否则 `send({ type: 'interrupt' })`

---

## 二、中等严重度问题 — 10/13 已修复

| # | 问题 | 文件 | 修复 |
|---|------|------|------|
| 1 | chatStore ↔ audioStore 循环依赖 | `chatStore.ts` / `audioStore.ts` | 双向文档化注释；`audioStore` 新增 `cleanup()` 方法 |
| 2 | orphaned 音频块内存泄漏 | `audioStore.ts` | `cleanup()` 清理 chunks + 撤销 Blob URLs |
| 4 | loadMoreMessages 无并发保护 | `chatStore.ts` | `_loadingMore` 标志位防护 |
| 5 | LTM/Archive 搜索无防抖 | `LtmPanel.vue` / `ArchivePanel.vue` | 300ms setTimeout/clearTimeout 防抖 |
| 6 | 全局 paste 监听器 | `InputBar.vue` | `document.addEventListener` → 组件根元素 `@paste` |
| 7 | VersionHistory 切换 contentType 时数据不刷新 | `VersionHistory.vue` | watch 增加 `contentType` |
| 10 | Lightbox.vue 非响应式 DOM 操作反模式 | `Lightbox.vue` | `classList.add/remove` → `v-if="lightboxImage"` |
| 12 | `send()` 返回值被所有调用者忽略 | `settingsStore.ts` | `switchModel` 检查返回值并 warn |
| 13 | debugStore 轮询间隔在恢复后永不重置 | `debugStore.ts` | `startPoll` 与 `restartPollTimer` 模式统一 |

---

## 三、低严重度问题 — 13/17 已修复

| # | 问题 | 涉及文件 | 修复 |
|---|------|----------|------|
| 1 | 图片上传逻辑重复 | `InputBar.vue` + `MessageList.vue` | 提取为 `composables/useImageUpload.ts` |
| 2 | `fmt()` 数字格式化函数重复 | `CharStatus.vue` + `TokenLabel.vue` | 提取为 `utils/format.ts` |
| 3 | 消息行 CSS 重复 | `TypingIndicator.vue` | 提取为 `styles/message-layout.css` |
| 4 | 硬编码中文 locale | `TimeDivider.vue` | 新增 `locale` prop 默认 `zh-CN` |
| 5 | 直接 store 属性赋值 vs `$patch` | `PreviewBar.vue` | 改用 `chatStore.removePreviewImage()` action |
| 7 | `doneTasks` 数组无限增长 | `TasksTab.vue` | 上限 100 条 |
| 8 | ConfirmModal 缺 Escape/焦点陷阱 | `ConfirmModal.vue` | Escape 关闭 + 自动聚焦确认按钮 |
| 9 | ModelSelector 只加载一次 provider 列表 | `ModelSelector.vue` | `refreshProviders()` + `defineExpose` |
| 10 | ToggleSwitch 无键盘可访问性 | `ToggleSwitch.vue` | `role="switch"` + `aria-checked` + `aria-label` + `@keydown` |
| 11 | TokenLabel 展开用 div 替代 button | `TokenLabel.vue` | `role="button"` + `tabindex="0"` + `@keydown` |
| 12 | ImageLightbox 缺加载状态/错误处理 | `ImageLightbox.vue` | 加载 spinner + 错误文本 + Escape 关闭 |
| 14 | 角色颜色映射不全 | `SessionPanel.vue` / `ArchivePanel.vue` | 补充 `system: '#888'` |
| 16 | ExpandableText Unicode 补充平面字符截断 | `ExpandableText.vue` | `[...text].slice()` 替代 `text.slice()` |
| 17 | ToolsPanel 按钮与 SettingsPanel 位置重叠 | `ToolsPanel.vue` / `SettingsPanel.vue` | 垂直分离（16px / 52px） |

---

## 四、架构改进 — 已完成

### 4.1 ✅ 提取共享 composable — 已完成

图片上传逻辑从 `InputBar.vue` / `MessageList.vue` 提取为 `composables/useImageUpload.ts`。返回 `{ uploadImage, onDragOver, onDragLeave, onDrop }`，支持可选的 `dropTarget` 回调用于 class 切换。

### 4.4 ✅ 提取共享 CSS（Debug 面板）— 已完成（`c912a8f` 后续）

`LtmPanel.vue`、`SessionPanel.vue`、`ArchivePanel.vue` 中 17 个重复选择器（`.panel`、`.toolbar`、`.btn`、`.status`、`.list-row` 等）提取为 `debug/styles/panel.css`。3 个面板通过非 scoped `<style>@import</style>` 引用，组件特有样式保留在各自 scoped 块中。Debug CSS 产物从 21.46KB → 17.84KB。

### 4.7 ✅ 统一 Store 状态突变模式 — 已完成

新增 `chatStore.removePreviewImage(url)` 和 `chatStore.clearPreviewImages()` actions，替代直接赋值模式。

### 4.3 ⬿️ 类型安全改进 — 部分完成

WebSocket 消息类型从 `as any` 迁移为 `ServerMessage` 判别联合 + 类型收窄。统一 messageRouter 仍需后续架构演进。

---

## 新增模块

| 文件 | 用途 |
|------|------|
| `composables/useImageUpload.ts` | InputBar + MessageList 共享上传/拖拽逻辑 |
| `utils/format.ts` | Token 计数格式化（CharStatus + TokenLabel 复用） |
| `styles/message-layout.css` | ChatMessage + TypingIndicator 共享消息布局 CSS |
| `debug/styles/panel.css` | Debug 面板共享样式（LtmPanel/SessionPanel/ArchivePanel 复用） |
| `frontend/README.md` | 前端完整文档（组件树、架构、技术要点） |

---

## 第二轮修复（`c912a8f`）

审查日期：2026-07-30 · 第二次审查发现 7 个新问题 + 7 个修复不完整项，全部修复。

### 关键修复（3 项）

| # | 问题 | 文件 | 修复 |
|---|------|------|------|
| 1 | `cleanup()` 定义但从未调用 | `audioStore.ts` / `connectionStore.ts` | `connectionStore.disconnect()` 中调用 `useAudioStore().cleanup()`，释放断线残留 chunks + Blob URLs |
| 2 | `finalizeStream` TTS 关闭时泄漏 | `audioStore.ts` | `delete audioChunks[streamId]` 移到 `if (!ttsEnabled) return` 之前 |
| 3 | InputBar 缺 dropTarget，拖拽无视觉反馈 | `InputBar.vue` | `useImageUpload(() => inputBarRef.value)` 传入 dropTarget；模板加 `ref="inputBarRef"` |

### 重要修复（4 项）

| # | 问题 | 文件 | 修复 |
|---|------|------|------|
| 4 | useImageUpload 子元素拖拽闪烁 | `useImageUpload.ts` | dragCounter 计数替代 class toggle；拆分为 `onDragOver`(preventDefault) + `onDragEnter`(counter++) |
| 5 | LtmPanel typeFilter 双次加载 | `LtmPanel.vue` | 移除 `<select @change="load">`，只靠 `watch([search, typeFilter], debouncedLoad)` 驱动 |
| 6 | ImageLightbox Escape 从不触发 | `ImageLightbox.vue` | 改用 `document.addEventListener('keydown', ...)` 在 visible watch 中注册/注销 |
| 7 | TasksTab reconnectTimer 竞争 | `TasksTab.vue` | `connect()` 开头 `clearTimeout(reconnectTimer)` |

### 修复不完整（3 项）

| # | 问题 | 文件 | 修复 |
|---|------|------|------|
| R2 | token_usage 仍 `as any` 双转换 | `useWebSocket.ts` | 改为 `m as TokenUsageMessage`；`chatStore.updateTokenUsage` 签名放松为可选字段 |
| R5 | ConfirmModal 缺 ARIA + 焦点陷阱 | `ConfirmModal.vue` | `role="dialog"` + `aria-modal` + `aria-labelledby` + Tab 焦点循环 |
| R6 | ChatMessage 仍重复 msg-row 样式 | `ChatMessage.vue` | 移除重复的 `.msg-row/.msg-avatar/.msg-bubble` scoped 样式，`@import` 共享 CSS |

### 健壮性改进（2 项）

| # | 问题 | 文件 | 修复 |
|---|------|------|------|
| R4 | LtmPanel 突变操作无错误处理 | `LtmPanel.vue` | 5 个方法（delete/save/create/clear/deleteSelected）全部 try/catch |
| R4 | ArchivePanel 突变操作无错误处理 | `ArchivePanel.vue` | 4 个方法（delete/save/clear/deleteSelected）全部 try/catch |

### 次要修复（2 项）

| # | 问题 | 文件 | 修复 |
|---|------|------|------|
| R7 | ImageLightbox 缺 alt 属性 | `ImageLightbox.vue` | `<img alt="图片预览">` |
| — | useWebSocket 各 case 分支显式类型收窄 | `useWebSocket.ts` | 每个 case 分支用局部变量 + `as NarrowType` 替代直接访问 `m.xxx` |
