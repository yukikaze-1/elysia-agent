# 前端代码审查 — TODO（剩余）

审查日期：2026-07-30
修复日期：2026-07-30 · 提交 `60ac3e5` + `99e821c`
**已修复 32/39 项 → 已归档至 [docs/frontend_completed.md](docs/frontend_completed.md)**

---

## 一、暂缓项

### 1. ⬜ 麦克风使用已废弃的 ScriptProcessorNode — InputBar.vue

`InputBar.vue` 使用 `audioCtx.createScriptProcessor()` 处理麦克风音频，此 API 已被 Web Audio 标准废弃，未来可能从浏览器中移除。

**修复方案：** 迁移至 `AudioWorklet`。
**阻塞原因：** AudioWorklet 需独立 `.js` 文件 + 复杂重构，优先级低于功能性修复。

---

### 2. ⬜ 无 AbortController — 多个组件

所有 fetch 请求（Debug 面板的轮询、LTM 查询、消息加载等）均未使用 `AbortController`。组件卸载后，网络回调仍会尝试更新已销毁的响应式状态。

**修复方案：** 为所有长时间运行的请求添加取消支持。
**阻塞原因：** 需全域架构变更，涉及多个组件和 store。

---

## 二、验证不成立 / 设计如此

| # | 问题 | 文件 | 原因 |
|---|------|------|------|
| P0-#9 | 音频缓冲区数据竞争 | `InputBar.vue` | `int16` 每次回调新建 `Int16Array` + 新 `ArrayBuffer`，不存在共享 |
| M-#3 | onclose 在新连接建立后仍触发重连 | `connectionStore.ts` | `connect()` 先 null 旧 socket 的 onclose 再 close |
| M-#8 | PromptViewer/ResponseViewer 双重请求 | `PromptViewer.vue` | `onMounted` 仅调 focus，不调 fetch；watch immediate 调 fetch |
| M-#9 | body.innerHTML 摧毁 Vue 应用 | `SettingsPanel.vue` | 重启/关机场景，页面即将 reload |
| M-#11 | disconnect 后 onclose 仍触发重连 | `connectionStore.ts` | onclose 在 close() 前 nulled |

---

## 三、低优先级暂缓

| # | 问题 | 涉及文件 | 原因 |
|---|------|----------|------|
| L-#6 | 无 AbortController | 多组件 | 同上，全域架构变更 |
| L-#13 | ArchivePanel 分页参数命名误导 | `ArchivePanel.vue` | API 契约不变，仅命名建议 |
| L-#15 | 硬编码颜色绕过 CSS 变量 | `variables.css:33` | 不影响功能，视觉无差异 |

---

## 四、架构建议（未实施）

### 4.2 迁移至 AudioWorklet

同暂缓项 #1。

### 4.3 建立统一的消息分发层

目前 WebSocket 消息流向是：

```
connectionStore.onmessage → _onMessage（unknown 类型）
→ useWebSocket.ts 的 handleMessage（ServerMessage 类型收窄）
→ 各 store 方法（appendDelta, setMood, setState 等）
```

类型安全已改进（`as any` → `ServerMessage`），但建议进一步建立 `messageRouter`，集中管理映射 + try/catch 边界。

### 4.4 提取共享 CSS（Debug 面板）

`LtmPanel.vue`、`SessionPanel.vue`、`ArchivePanel.vue` 之间存在大量重复样式（`.panel`、`.toolbar`、`.field`、`.btn`、`.btn-sm` 等）。可提取为共享 CSS 文件。

### 4.5 建立持久化管理模式

`settingsStore.ts` 中 5 组 localStorage watch 模式完全相同，可提取为 `useLocalStorage` composable 或用 `pinia-plugin-persist`。

### 4.6 添加 Vue 错误边界

目前没有任何 `errorCaptured` 钩子或全局 `app.config.errorHandler` 配置。建议至少添加根级错误边界。

### 4.8 添加请求取消机制

同暂缓项 #2。所有 fetch 请求需 `AbortController` 支持。
