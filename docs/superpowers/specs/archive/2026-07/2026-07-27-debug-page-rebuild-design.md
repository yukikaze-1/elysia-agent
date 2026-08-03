# Debug Page 完整重建 — 设计规格

## 概述

Vue 3 迁移时，debug 仪表盘（原 `static/debug.html`，2238 行 vanilla JS）只重建了骨架（`frontend/src/debug/App.vue`，122 行）。需要 1:1 恢复旧版全部功能。

**目标**：三个 Tab（概览 16 面板、数据库 CRUD、任务 SSE 流）+ 全部 Modal 查看器。

## 架构

```
src/debug/
├── main.ts                          # 入口：createApp + createPinia
├── App.vue                          # Shell: tab bar + pause/resume + copy JSON full/light
├── types.ts                         # DebugData 等 TS 接口定义
├── components/
│   ├── OverviewTab.vue              # 16 panels in 2-column CSS grid
│   ├── DatabaseTab.vue              # Container, loads data on tab switch
│   ├── LtmPanel.vue                 # Search/filter/paginate/CRUD/batch/clear (Milvus)
│   ├── SessionPanel.vue             # List/detail/delete/clear (MongoDB sessions)
│   ├── ArchivePanel.vue             # Search/filter/paginate/edit/delete/clear (MongoDB archive)
│   ├── TasksTab.vue                 # EventSource SSE + auto-reconnect + active/done sections
│   ├── TaskCard.vue                 # Status badge + progress bar + vis-network DAG
│   ├── shared/
│   │   ├── PaginationBar.vue        # ← → page nav + jump-to-page input
│   │   ├── ConfirmModal.vue         # "Are you sure?" gate for destructive ops
│   │   └── ExpandableText.vue       # Truncate + click-to-expand long text
│   └── modals/
│       ├── PromptViewer.vue         # Collapsible messages + template sections + tools list
│       ├── ResponseViewer.vue       # inner_voice display + raw text
│       ├── VersionHistory.vue       # Paginated version list + rollback button
│       └── ImageLightbox.vue        # Fullscreen image overlay
└── stores/
    └── debugStore.ts                # Pinia: data, activeTab, paused, page params
```

### 新增依赖

- `vis-network` + `vis-data` — Tasks Tab DAG 渲染（npm）

### TypeScript 类型

**`src/debug/types.ts`** — 与服务端 `DebugInfo` dataclass 对应的完整类型定义：

```ts
// ---- 子结构（被 DebugData 引用） ----

interface TokenRoundItem { model: string; prompt_tokens: number; completion_tokens: number; total_tokens: number; round_num: number }
interface TokenStats { session_total: number; per_model: Record<string, number>; current_turn: TokenRoundItem[] }
interface TimingStats { count: number; avg_ms: number; p50_ms: number; p95_ms: number; p99_ms: number; last_ms: number }
/** 进程内存（rss/vms/pct/threads），不是"长期记忆" */
interface ProcessMemory { rss_mb: number; vms_mb: number; pct: number; num_threads: number }
interface PromptRecord { index: number; timestamp: string; round_num: number; message_count: number; system_prompt_len: number; total_chars: number; model: string }
interface ResponseRecord { index: number; timestamp: string; round_num: number; text_len: number; inner_voice_len: number }
/** session_recent 条目：后端直接提供的字段 */
interface SessionRecentItem { role: string; parts: { content: string }[]; content: ContentPart[]; content_type?: Record<string, unknown> }
interface ContentPart { type: string; content?: string; src?: string; mime?: string }
interface LtmRecentItem { id: string; text: string; memory_type: string; poignancy: number; metadata?: Record<string, unknown> }
interface ToolItem { name: string; description: string; parameters?: Record<string, unknown> }
interface ToolHistoryItem { name: string; args?: unknown; result?: unknown; error?: string }
interface ConsolidationHistoryItem { type: string; content?: string; [key: string]: unknown }

// ---- 分组子对象（让面板直接传子对象，减少 prop drilling） ----

interface ConsolidationInfo {
  buffer_size: number; threshold: number
  history: ConsolidationHistoryItem[]
}
interface MemoryReflectorInfo {
  exists: boolean; round_count: number; round_threshold: number
  new_memory_count: number; memory_threshold: number
  locked: boolean; last_summary?: unknown
}
interface CharacterReflectorInfo {
  exists: boolean; buffer_size: number
  last_reflection_at?: number; locked: boolean
  self_perception_history?: unknown[]
  last_prompt_system?: string; last_prompt_user?: string
  last_text_len?: number; last_duration_ms?: number; last_success?: boolean
}
interface CharacterEngineInfo {
  exists: boolean; self_l2: string; state_l3: string; mood: string
  last_awareness_time?: number; l2_version_count: number; c6_version_count: number
  state_history?: unknown[]
}

// ---- DebugData（与后端 DebugInfo dataclass 字段一一对应，平铺结构） ----

interface DebugData {
  fsm_state: string; fsm_previous: string | null; uptime_seconds: number; processing: boolean
  llm_model: string; tts_ok: boolean; stt_ok: boolean; ws_clients: number
  proactive_enabled: boolean; proactive_interval?: number; last_interaction_ago?: number
  session_messages: number; session_recent: SessionRecentItem[]
  // 完整的 character state JSON dump（session 持久化数据），debug 用，点击可展开查看
  character_state_raw: Record<string, unknown>
  character_name: string; base_personality_summary: string
  archive_msg_count: number; lt_count: number; lt_recent: LtmRecentItem[]
  milvus_ok: boolean; embedding_ok: boolean; mongodb_ok: boolean
  currently_executing: string[]; tools: ToolItem[]; tool_history: ToolHistoryItem[]

  // 以下字段为后端平铺输出，面板 #13-16 通过 computed 提取拼成子对象传入
  consolidator_buffer_size: number; consolidator_threshold: number
  consolidation_history: ConsolidationHistoryItem[]

  mreflector_exists: boolean; mreflector_round_count: number; mreflector_round_threshold: number
  mreflector_new_memory_count: number; mreflector_memory_threshold: number
  mreflector_locked: boolean; mreflector_last_summary: unknown

  reflector_exists: boolean; reflector_buffer_size: number
  reflector_last_reflection_at: number | null; reflector_locked: boolean
  self_perception_history: unknown[]
  reflector_last_prompt_system: string; reflector_last_prompt_user: string
  reflector_last_success: boolean; reflector_last_duration_ms: number; reflector_last_text_len: number

  character_engine_exists: boolean; self_l2: string; state_l3: string
  state_history: unknown[]; last_awareness_time: number
  l2_version_count: number; c6_version_count: number

  token_stats: TokenStats; pending_token_count: number; pending_session_exists: boolean
  prompt_summary: { records: PromptRecord[]; total: number; max: number }
  response_summary: { records: ResponseRecord[]; total: number; max: number }
  timing_history: unknown[]; timing_stats: TimingStats
  /** 进程内存（不是"长期记忆"） */
  memory: ProcessMemory
  mood: string
  scheduler_pending: number; scheduler_running: number; scheduler_total: number; task_tree: unknown
}
```

面板 #13-16 的 props 传递方式：
```ts
// OverviewTab.vue 中，每个面板通过 computed 从 store.data 提取对应字段：
const consolidationProps = computed(() => ({
  buffer_size: store.data!.consolidator_buffer_size,
  threshold: store.data!.consolidator_threshold,
  history: store.data!.consolidation_history,
}))
// → <ConsolidationPanel :data="consolidationProps" />
```
分组 interface（ConsolidationInfo, MemoryReflectorInfo 等）仅用于面板 props 类型标注，不对应 API 响应形状。
```

### CSS 策略

- **全局 style**：App.vue 中一个非 scoped `<style>` 块覆盖 body（背景色、字体），确保不影响主应用的 `body { overflow: hidden; font-family }`。设置 debug 页面自己的 `body { background: var(--debug-bg); color: var(--debug-text); padding: 20px; overflow: auto; }`
- **CSS 变量**：在 App.vue 全局 style 中定义 `--debug-bg`, `--debug-surface`, `--debug-border`, `--debug-text`, `--debug-muted`, `--debug-accent`, `--debug-green`, `--debug-warn`, `--debug-red`，和旧版值保持一致
- **组件级样式**：每个组件用 `<style scoped>`，遵循项目约定
- **不需要** 独立的 debug variables.css 文件 — 变量少，直接在 App.vue 定义即可，避免多一个网络请求

## 组件规格

### App.vue (Shell)

**职责**：Tab 导航 + 全局控制 + 切换渲染

**状态**：
- `activeTab` — from store: `'overview' | 'database' | 'tasks'`
- `paused` — from store: 暂停/恢复轮询

**Template**：
- 顶部栏：Tab 按钮组（概览 | 数据库 | 任务）+ pause 按钮 + copy JSON full + copy JSON light
- 内容区：`<component :is="activeTabComponent">` 动态切换

**行为**：
- 切换 Tab 时更新 store.activeTab
- pause 时停止 store.poll()，显示 "已暂停" 指示器
- copy JSON：`navigator.clipboard.writeText(JSON.stringify(store.data))`
- copy JSON light：同上但 `JSON.stringify` 用 2-space indent

---

### debugStore.ts (Pinia)

**状态**：
```ts
data: DebugData | null           // GET /debug/api 快照
activeTab: 'overview' | 'database' | 'tasks'
paused: boolean

// 分页 — 统一使用 offset
// session 数量通常很少（几个到十几个），后端 list_all() 无分页，因此不做翻页
ltmPage: { limit: 50, offset: 0 }
archivePage: { limit: 100, offset: 0 }
```

**Actions**：
- `fetchDebugData()` — GET /debug/api，更新 data
- `startPoll()` — 立即调用一次 fetchDebugData()，然后 setInterval 2s。错误时指数退避到 30s
- `stopPoll()` — clearInterval
- **初始化**：store 创建后，若 activeTab === 'overview'（默认值），自动调用 startPoll()
- `watch(activeTab)` — 切换到 overview 时 startPoll，切走时 stopPoll
- `watch(paused)` — true → stopPoll；false → startPoll（触发立即刷新 + 恢复定时器）

---

### OverviewTab.vue

**职责**：16 面板 2 列 grid 渲染。所有数据来自 `store.data`。

**面板明细**：

| # | 面板 | 关键字段 | 特殊处理 |
|---|------|---------|---------|
| 1 | System | fsm_state, fsm_previous, uptime_seconds, llm_model, tts_ok, stt_ok, ws_clients, proactive_enabled, last_interaction_ago | FSM idle=绿，其他=橙 |
| 2 | Memory | session_messages, lt_count, mongodb_ok, milvus_ok, embedding_ok, archive_msg_count, base_personality_summary | DB 健康 ✓/✗，personality 截断可展开，character_state_raw 可展开 |
| 3 | Latency | timing_stats (count, avg_ms, p50, p95, p99, last_ms) | 无数据时隐藏面板 |
| 4 | Process Memory | memory (rss_mb, vms_mb, pct, num_threads) | |
| 5 | Scheduler | scheduler_pending, scheduler_running, scheduler_total, task_tree | task_tree JSON 截断可展开 |
| 6 | Token | token_stats (session_total + per_model), pending_token_count, pending_session_exists | pending 显示警告 |
| 7 | LLM Prompt | prompt_summary.records (最近 3: index, round, msgs, chars, model, time) | "View" → PromptViewer modal |
| 8 | LLM Response | response_summary.records (最近 3: index, round, text_len, inner_voice_len, time) | "View" → ResponseViewer modal |
| 9 | Recent Session | session_recent (role, parts, content, content_type) — inner_voice 和图片从 content 数组客户端提取 | role 色标；`content` 中 `type: "image"` 条目 → ImageLightbox；`content_type` 中如有 inner_voice 标记则渲染为 italic 样式 |
| 10 | LTM Recent | lt_recent (text, memory_type, poignancy) | type 色码标签 |
| 11 | Tools | tools (name, description, params) | 前 4 个,其余可展开；executing 红色横幅 |
| 12 | Tool History | tool_history (name, args, result/error) | error 红色 |
| 13 | Consolidation | consolidator_buffer_size/threshold + consolidation_history → 经 computed 组装为 ConsolidationInfo props | 接近阈值时警告 |
| 14 | Memory Reflector | mreflector_* 字段 → 经 computed 组装为 MemoryReflectorInfo props | 百分比条 |
| 15 | Character Reflector | reflector_* 字段 → 经 computed 组装为 CharacterReflectorInfo props | 可展开 prompt |
| 16 | CharacterEngine | character_engine_exists/self_l2/state_l3/... → 经 computed 组装为 CharacterEngineInfo props | 版本号 → VersionHistory modal |

**色码**：Fact=#4caf50、Plan=#2196f3、Preference=#9c27b0、Event=#ff9800、Note=#888

**可选 UX 增强**：每个面板加 `id` anchor（如 `id="panel-system"`），顶部添加 mini nav 快速跳转。非必须，酌情实现。

---

### DatabaseTab.vue

**职责**：三个子面板容器。仅在 activeTab === 'database' 时渲染。

**Template**：
```
<section>
  <LtmPanel />
  <SessionPanel />
  <ArchivePanel />
</section>
```

---

### LtmPanel.vue

**API**：`GET/POST/PUT/DELETE /debug/api/ltm`, `DELETE /debug/api/ltm`

**状态**（组件内 local ref）：
- entries, total, loading, error, search, typeFilter, selectedIds

**状态机**：
| 状态 | 渲染 |
|------|------|
| `loading` | 加载 spinner + "加载中..." |
| `error` | 红色错误横幅 + 具体错误信息 + "重试"按钮 |
| `empty` | "暂无记忆数据"，若设置了筛选条件则显示 "当前筛选条件下无结果" |
| `data` | 正常列表 |

**Template**：

- 工具栏：search `<input>` + type `<select>` + "添加新记忆"按钮 + "清空全部"按钮
- 列表：每行 checkbox + type 色标 + 展开文本 + timestamp + poignancy + 编辑按钮（内联 input → PUT）+ 删除按钮（→ ConfirmModal → DELETE）
- 底部：全选/取消全选 + "删除选中"按钮
- PaginationBar：perPage 50/100/200 + 页码 + 跳转

**新增 Modal**：text `<textarea>` + type `<select>` + poignancy slider (1-10) + 保存按钮 → POST

**分页参数**：`limit`（映射 perPage）、`offset`（映射 page × perPage）

### SessionPanel.vue

**API**：`GET /debug/api/sessions`, `GET /debug/api/sessions/:id`, `DELETE /debug/api/sessions/:id`, `DELETE /debug/api/sessions`

**状态机**：
| 状态 | 渲染 |
|------|------|
| `loading` | 加载 spinner |
| `error` | 错误横幅 + 重试按钮 |
| `empty` | "暂无会话" |
| `data` | 会话列表 |

**Template**：
- 列表：每行 checkbox + session_id (截断) + message_count + token_total + updated_at + "查看详情"按钮
- 详情展开：内联渲染消息（role 标签 + inner_voice + 展开文本 + 图片）
- 操作：全选/取消全选 + 删除选中 + 清空全部（→ ConfirmModal）

### ArchivePanel.vue

**API**：`GET /debug/api/archive`, `PUT/DELETE /debug/api/archive/:id`, `DELETE /debug/api/archive`

**状态机**：
| 状态 | 渲染 |
|------|------|
| `loading` | 加载 spinner |
| `error` | 错误横幅 + 重试按钮 |
| `empty` | "暂无归档消息"，如有筛选则显示筛选上下文 |
| `data` | 消息列表 |

**Template**：
- 工具栏：search `<input>` + role `<select>`（user/assistant/tool/all）
- 列表：每行 checkbox + role 色标 + timestamp + 展开文本 + 编辑按钮（内联 input → PUT）+ 删除按钮
- 操作：全选/取消全选 + 删除选中 + 清空全部
- PaginationBar

**分页参数映射**：Archive API 使用 `skip`，PaginationBar 内部统一使用 `offset`。ArchivePanel 在调用 API 时将 `offset` 映射为 `skip`。

---

### TasksTab.vue

**API**：`GET /debug/api/tasks/stream` (SSE `text/event-stream`)

**生命周期**：
- `onMounted` → `new EventSource('/debug/api/tasks/stream')`
- `onUnmounted` → `.close()`

**事件处理**：
| SSE Event | Action |
|-----------|--------|
| snapshot | 全量替换 tasks |
| task_new | unshift 到活跃列表 |
| task_update | 更新 task 的 status + nodes + DAG 色块 |
| task_done | 从活跃移除，push 到完成区 |
| node_update | 更新单个节点颜色 |
| heartbeat | 忽略 |

**自动重连**：onerror → 1s/2s/4s...30s 退避重连，指示器绿/黄/红

**Template**：
- 连接状态指示器（圆点 + 文字）
- 活跃任务列表（TaskCard × N）
- `<details>` 完成区（精简卡片，无 DAG）

---

### TaskCard.vue

**Props**：task 对象 `{ task_id, status, plan_version, created_at, error, error_code, phase, mailbox_depth, nodes: TaskNode[], edges: [string, string][] }`

**TaskNode**：`{ node_id, status, attempt, summary, error }`

**Template**：
- task_id (截断) + status 色标（running=#42a5f5, completed=#66bb6a, failed=#ef5350, cancelled=#bdbdbd）
- plan_version + node_count + mailbox_depth
- 进度条 `(success + skipped) / total`
- **vis-network 容器**：
  - 布局方向：`rankdir: 'TB'`（上→下），hierarchical layout
  - 最大节点数 ~200，禁用物理引擎（`physics: false`）以保证性能
  - 节点色码映射：

| node.status | 颜色 |
|-------------|------|
| pending | `#ffa726` (orange) |
| running | `#42a5f5` (blue) |
| success | `#66bb6a` (green) |
| failed | `#ef5350` (red) |
| skipped | `#bdbdbd` (grey) |

  - 边：带箭头 `arrows: 'to'`
  - 容器 `<div>` 设定 `height: 300px` + `border: 1px solid var(--debug-border)`

---

### Shared UI

**PaginationBar.vue**
- Props: `total`（总条目数）, `perPage`, `currentOffset`
- 内部计算：`totalPages = Math.ceil(total / perPage)`, `currentPage = Math.floor(currentOffset / perPage) + 1`
- Emits: `update:perPage`, `goToOffset(offset)`
- 统一使用 `offset` 语义（emit `offset`，父组件直接写入 store 的 `offset`）
- ArchivePanel 在调用 API 时将 `offset` 映射为 `skip` 参数
- Template：prev/next 按钮 + "第 N/M 页" + perPage 下拉（50/100/200）+ "前往第" input

**ConfirmModal.vue**
- Props: `message`, `visible`
- Emits: `confirm`, `cancel`
- Teleport to body，半透明遮罩 + 居中确认框

**ExpandableText.vue**
- Props: `text`, `maxLength` (default 120)
- 切换展开/收起，带 "..." 指示

---

### Modals

**PromptViewer.vue**
- 来自 Overview Prompt 面板 "View" 按钮
- Fetch `/debug/api/prompts/:index`
- 左栏：可折叠消息列表（system 默认展开），每条带 size badge + copy
- 右栏：template sections（可折叠 + copy per section + "copy all"）
- 底部：工具列表卡片 + copy as JSON/text
- ← → 箭头导航 prompt 历史

**ResponseViewer.vue**
- Fetch `/debug/api/responses/:index`
- inner_voice（斜体灰色 + 左边框）
- raw text `<pre>`
- ← → 箭头导航

**VersionHistory.vue**
- 来自 CharacterEngine 面板版本计数点击
- Fetch `/debug/api/character-versions?type=l2|c6&limit=20&before=N`（before 用于光标分页）
- 列表：timestamp + trigger + text preview + change_summary + rollback 按钮
- rollback → POST `/debug/api/character-rollback`，body: `{ type: "l2" | "c6", version: <int> }`

**ImageLightbox.vue**
- Teleport to body
- 全屏半透明背景 + `<img>` + 点击关闭
- **图片来源**：`session_recent[n].content` 数组中的 `{ type: "image", src: "/api/images/..." }` 条目。src 指向服务端图片路由

## 数据流

```
App.vue
  └─ debugStore (Pinia)
       ├─ data: GET /debug/api ← poll every 2s (overview tab only)
       │    └─ consumed by OverviewTab + PromptViewer + ResponseViewer + VersionHistory
       ├─ activeTab: controls which component renders + poll lifecycle
       └─ paused: inhibits poll

DatabaseTab children — each fetches independently:
  LtmPanel      → GET/POST/PUT/DELETE /debug/api/ltm*
  SessionPanel  → GET/DELETE /debug/api/sessions*
  ArchivePanel  → GET/PUT/DELETE /debug/api/archive*

TasksTab  → EventSource GET /debug/api/tasks/stream (SSE)
```

## 错误处理

- 所有 fetch 调用 catch → 内联错误提示（不阻塞 UI）
- 网络错误：轮询指数退避（2s → 30s），显示 "上次刷新失败" 状态
- SSE 断开：自动重连 + 连接状态指示器
- Destructive ops：全部走 ConfirmModal
- 空状态：列表为空时显示 "暂无数据"

## 验证

1. `cd frontend && npm install && npm run build` — 无类型错误，构建成功
2. 构建产物：`static/debug.html` + `static/assets/debug-*.js` + `static/assets/debug-*.css`
3. 启动服务 `python -m core`（或 `make run`），访问 `/debug`：
   - 三个 Tab 点击切换正常
   - Overview 16 面板数据渲染正确
   - Database: LTM 搜索/翻页/CRUD，Session 列表/详情，Archive 搜索/编辑
   - Tasks: SSE 连接成功，vis-network DAG 渲染
   - Prompt/Response Viewer modal 左右导航 + 复制
   - VersionHistory modal 列表 + rollback
   - ImageLightbox 点击放大
