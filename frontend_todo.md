# 前端代码审查 — TODO（剩余）

审查日期：2026-07-30
修复日期：2026-07-30 · 提交 `60ac3e5` + `99e821c`
**已修复 32/39 项 → 已归档至 [docs/frontend_completed.md](docs/frontend_completed.md)**

第二次审查（2026-07-30）发现 **7 个新问题** + **7 个修复不完整项**。

---

## 一、关键问题 — 第二轮新发现

### 1. 🔴 `audioStore.cleanup()` 定义但从未调用 — 每次断线泄漏音频内存

`audioStore.ts:114-129` 添加了 `cleanup()` 方法（释放 `audioChunks` + 撤销 Blob URL），但全代码库没有任何地方调用它。每次 WS 断线重连都会累积上一连接的部分音频流数据。

**修复方案：** 在 `connectionStore.disconnect()` 中懒导入 `useAudioStore().cleanup()`。

---

### 2. 🔴 `finalizeStream` 在 TTS 关闭时仍泄漏音频块

`audioStore.ts:32-63` — `if (!settingsStore.ttsEnabled) return` 提前退出，导致 `delete audioChunks.value[streamId]` 永不执行。TTS 关闭期间所有流累积在 `audioChunks` 中无法释放。

**修复方案：** 将 `delete audioChunks.value[streamId]` 移到 early return 之前。

---

### 3. 🔴 `InputBar.vue` 缺少 `dropTarget` — 文本框拖放视觉反馈完全失效

`InputBar.vue:30` — `useImageUpload()` 调用时没有传 `dropTarget` 参数。对比 `MessageList.vue:18` 正确传了 `useImageUpload(() => chatWrapRef.value)`。拖拽文件到输入框上没有视觉反馈（`drag-over` class 永远不会被添加）。

**修复方案：** 传入文本区域的 dropTarget 函数。

---

## 二、重要问题 — 第二轮新发现

### 4. 🟡 `useImageUpload` — 子元素 dragenter/dragleave 导致闪烁

`useImageUpload.ts:30-48` — 经典拖拽 anti-pattern：当光标经过子元素时，父容器的 `dragleave` + `dragenter` 交替触发导致 `drag-over` class 快速闪烁。

**修复方案：** 使用 `dragCounter` 计数替代单纯的 class toggle。

---

### 5. 🟡 `LtmPanel.vue` — typeFilter 下拉框双次加载

第 134 行 `<select @change="load">` + 第 67 行 `watch([search, typeFilter], debouncedLoad)` 导致切换筛选类型时触发两次 API 请求：第一次用旧 offset 立即加载，第二次 300ms 后 offset 归零再加载。

**修复方案：** 删除 `<select>` 上的 `@change="load"`，只靠 watcher 驱动（与 ArchivePanel 一致）。

---

### 6. 🟡 `ImageLightbox.vue` — Escape key 处理器绑定在不可聚焦的 div 上

Escape handler 在 `div.lightbox-overlay` 上（第 27 行），但该 div 没有 `tabindex`。按键事件不会冒泡到这个 div，Escape handler 从未触发。

**修复方案：** 给 overlay 添加 `tabindex="-1"` 并在 `visible` 为 `true` 时 `.focus()`；或使用 `document.addEventListener('keydown', ...)` 并在 watcher 中注册/注销。

---

### 7. 🟡 `TasksTab.vue` — 残留 `reconnectTimer` 导致重复 SSE 连接

`connect()` 函数没有在开头 `clearTimeout(reconnectTimer)`。如果 `onerror` 设置了重连定时器，期间 `connect()` 被再次调用，旧定时器完成后会创建第二个并行 EventSource。

**修复方案：** 在 `connect()` 开头 `if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }`。

---

## 三、修复不完整 — 第二轮仍存在

| # | 问题 | 文件 | 严重度 |
|---|------|------|--------|
| R1 | `document.body.innerHTML = '...'` 摧毁 Vue 应用 | `SettingsPanel.vue` | M |
| R2 | `token_usage` 处理仍有 `as any` 双转换 | `useWebSocket.ts:82` | M |
| R3 | TimeDivider locale 默认仍硬编码 `zh-CN` | `TimeDivider.vue` | L |
| R4 | LtmPanel/ArchivePanel 突变操作无错误处理 | `LtmPanel.vue` / `ArchivePanel.vue` | L |
| R5 | ConfirmModal 缺焦点陷阱 + ARIA 属性 | `ConfirmModal.vue` | L |
| R6 | ChatMessage 仍重复 .msg-row/.msg-avatar/.msg-bubble 样式 | `ChatMessage.vue:129-169` | L |
| R7 | ImageLightbox 缺 `alt` 属性 | `ImageLightbox.vue` | L |

---

## 四、暂缓项（第一轮遗留）

### 1. ⬜ 麦克风使用已废弃的 ScriptProcessorNode — InputBar.vue

`InputBar.vue` 使用 `audioCtx.createScriptProcessor()` 处理麦克风音频，此 API 已被 Web Audio 标准废弃，未来可能从浏览器中移除。

**修复方案：** 迁移至 `AudioWorklet`。
**阻塞原因：** AudioWorklet 需独立 `.js` 文件 + 复杂重构，优先级低于功能性修复。

---

### 2. ⬜ 无 AbortController — 多个组件

所有 fetch 请求（Debug 面板的轮询、LTM 查询、消息加载等）均未使用 `AbortController`。组件卸载后，网络回调仍会尝试更新已销毁的响应式状态。

**阻塞原因：** 需全域架构变更，涉及多个组件和 store。

---

## 五、验证不成立 / 设计如此（第一轮）

| # | 问题 | 文件 | 原因 |
|---|------|------|------|
| P0-#9 | 音频缓冲区数据竞争 | `InputBar.vue` | `int16` 每次回调新建 `Int16Array` + 新 `ArrayBuffer`，不存在共享 |
| M-#3 | onclose 在新连接建立后仍触发重连 | `connectionStore.ts` | `connect()` 先 null 旧 socket 的 onclose 再 close |
| M-#8 | PromptViewer/ResponseViewer 双重请求 | `PromptViewer.vue` | `onMounted` 仅调 focus，不调 fetch；watch immediate 调 fetch |
| M-#9 | body.innerHTML 摧毁 Vue 应用 | `SettingsPanel.vue` | 重启/关机场景，页面即将 reload |
| M-#11 | disconnect 后 onclose 仍触发重连 | `connectionStore.ts` | onclose 在 close() 前 nulled |

---

## 六、低优先级暂缓（第一轮）

| # | 问题 | 涉及文件 | 原因 |
|---|------|----------|------|
| L-#6 | 无 AbortController | 多组件 | 同上，全域架构变更 |
| L-#13 | ArchivePanel 分页参数命名误导 | `ArchivePanel.vue` | API 契约不变，仅命名建议 |
| L-#15 | 硬编码颜色绕过 CSS 变量 | `variables.css:33` | 不影响功能，视觉无差异 |

---

## 七、架构建议（未实施）

### 7.2 迁移至 AudioWorklet

同暂缓项 #1。

### 7.3 建立统一的消息分发层

```
connectionStore.onmessage → _onMessage（unknown 类型）
→ useWebSocket.ts 的 handleMessage（ServerMessage 类型收窄）
→ 各 store 方法（appendDelta, setMood, setState 等）
```

类型安全已改进（大部分 `as any` → `ServerMessage`），但 `token_usage` 仍用 `as any`。建议进一步建立 `messageRouter`，集中管理映射 + try/catch 边界。

### 7.4 提取共享 CSS（Debug 面板）

`LtmPanel.vue`、`SessionPanel.vue`、`ArchivePanel.vue` 之间存在大量重复样式。可提取为共享 CSS 文件。

### 7.5 建立持久化管理模式

`settingsStore.ts` 中 5 组 localStorage watch 模式完全相同，可提取为 `useLocalStorage` composable 或用 `pinia-plugin-persist`。

### 7.6 添加 Vue 错误边界

目前没有任何 `errorCaptured` 钩子或全局 `app.config.errorHandler` 配置。建议至少添加根级错误边界。

### 7.8 添加请求取消机制

同暂缓项 #2。所有 fetch 请求需 `AbortController` 支持。

---

## 优先级总览

| 批次 | 范围 | 预计工作量 |
|------|------|-----------|
| **P0** | 关键 #1 (cleanup 未调用), #2 (TTS-off 泄漏), #3 (InputBar dropTarget) | 1 小时 |
| **P1** | 重要 #4~#7 + 修复不完整 R1~R2 | 2 小时 |
| **P2** | 修复不完整 R3~R7 | 2 小时 |
| **P3** | 暂缓项 + 架构建议 | 2 天 |
