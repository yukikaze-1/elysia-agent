# 前端代码审查 — TODO（剩余）

审查日期：2026-07-30
修复日期：2026-07-30 · 提交 `60ac3e5` + `99e821c`（第一轮）+ `c912a8f`（第二轮）
**已修复 42/47 项 → 归档至 [docs/review/frontend_completed.md](docs/review/frontend_completed.md)**

---

## 一、暂缓项

### 1. ⬜ 麦克风使用已废弃的 ScriptProcessorNode — InputBar.vue

`InputBar.vue` 使用 `audioCtx.createScriptProcessor()` 处理麦克风音频，此 API 已被 Web Audio 标准废弃，未来可能从浏览器中移除。

**阻塞原因：** AudioWorklet 需独立 `.js` 文件 + 复杂重构。

---

### 2. ✅ AbortController — 已完成

8 个文件已集成 `composables/useAbortController.ts`：LtmPanel、ArchivePanel、SessionPanel、PromptViewer、ResponseViewer（A 类防竞态）+ ModelSelector、debugStore（B 类卸载清理）。

---

## 二、验证不成立 / 设计如此

| # | 问题 | 文件 | 原因 |
|---|------|------|------|
| P0-#9 | 音频缓冲区数据竞争 | `InputBar.vue` | `int16` 每次回调新建 `Int16Array` + 新 `ArrayBuffer` |
| M-#3 | onclose 在新连接建立后仍触发重连 | `connectionStore.ts` | `connect()` 先 null onclose 再 close |
| M-#8 | PromptViewer/ResponseViewer 双重请求 | `PromptViewer.vue` | `onMounted` 仅调 focus，不调 fetch |
| M-#9 | body.innerHTML 摧毁 Vue 应用 | `SettingsPanel.vue` | 重启/关机场景，页面即将 reload |
| M-#11 | disconnect 后 onclose 仍触发重连 | `connectionStore.ts` | onclose 在 close() 前 nulled |
| R3 | TimeDivider locale 默认 `zh-CN` | `TimeDivider.vue` | prop 默认值是正确的模式，可被父组件覆盖 |

---

## 三、低优先级暂缓

| # | 问题 | 涉及文件 | 原因 |
|---|------|----------|------|
| L-#6 | 无 AbortController | 多组件 | ✅ 已完成 — A+B 类全部集成 |
| L-#13 | ArchivePanel 分页参数命名误导 | `ArchivePanel.vue` | API 契约不变，仅命名建议 |
| L-#15 | 硬编码颜色绕过 CSS 变量 | `variables.css:33` | 不影响功能 |

---

## 四、架构建议（未实施）

### 4.1 迁移至 AudioWorklet

同暂缓项 #1。

### 4.2 建立统一的消息分发层

类型安全已大幅改进（每个 case 分支显式类型收窄），但建议进一步建立 `messageRouter` 集中管理映射 + try/catch 边界。

### 4.3 ✅ 提取共享 CSS（Debug 面板）— 已完成

`LtmPanel.vue`、`SessionPanel.vue`、`ArchivePanel.vue` 之间 17 个重复选择器已提取为 `debug/styles/panel.css`。提交 `c912a8f` 后续。

### 4.4 建立持久化管理模式

`settingsStore.ts` 中 5 组 localStorage watch 模式完全相同，可提取为 `useLocalStorage` composable。

### 4.5 添加 Vue 错误边界

目前没有任何 `errorCaptured` 钩子或全局 `app.config.errorHandler`。

### 4.6 ✅ 添加请求取消机制 — 已完成

同暂缓项 #2。8 文件集成 `composables/useAbortController.ts`。
