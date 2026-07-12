# Debug Dashboard

`GET /debug` — Elysia Agent 内部状态监控仪表板。单文件 HTML + CSS + JS，无依赖（除 vis-network CDN 用于 Task DAG 可视化）。

## 访问

```
http://localhost:8765/debug
```

数据源：每 2 秒轮询 `GET /debug/api`（指数退避至 30s）。

## 标签页

### 概览

12+ 个面板，覆盖所有 Agent 子系统：

| 面板 | 内容 |
|------|------|
| **System** | FSM 状态、Uptime、LLM 型号、TTS/STT、WS 连接数、Proactive 开关 |
| **Memory** | Session 消息数、LTM 条目数、MongoDB/Milvus/Embedding 状态、Archive 计数、Character 名、L1 Core 摘要 |
| **Latency** | 请求延迟统计（avg / P50 / P95 / P99 / last） |
| **Process Memory** | RSS / VMS / 占比 / 线程数 |
| **Scheduler** | Worker Pool 状态（pending / running / total） |
| **Token 消耗** | Session 总计 + 按模型分布 + pending 警告 |
| **LLM Prompt** | 最近 3 条 prompt 记录，点击查看完整消息列表 + 12 section 模板（可折叠、可复制） |
| **LLM Raw Response** | 最近 3 条原始回复（含 inner_voice），点击查看详情 |
| **Session** | 最近 5 条消息 + 图片渲染 |
| **Long-term Memory** | 最近 5 条记忆（类型标签 + 重要性） |
| **Tools** | 工具列表（默认显示 4 个，可展开）+ 执行状态 |
| **Tool History** | 最近 5 次工具调用 |
| **Consolidation** | 最近 5 条记忆沉淀 |
| **Character Reflector** | Buffer 进度、上次反思时间、Self-Perception / State 历史、最近 Prompt |
| **PersonalityEngine** | L2 Self、L3 State、C9 冷却状态、State 历史 |

右上角按钮：
- **暂停/恢复** — 停止/恢复轮询
- **复制 JSON** — 完整 DebugInfo JSON
- **精简 JSON** — 省略 session_params，session_recent 截断至 3 条

### 数据库

三个可折叠面板：

**Long-Term Memory (Milvus)**

| 功能 | 说明 |
|------|------|
| 搜索 | 全文搜索（服务端，MongoDB regex） |
| 类型过滤 | Fact / Plan / Preference / Event / Note 下拉 |
| 分页 | 50/100/200 条/页，上一页/下一页/跳页 |
| 复选框 | 全选/取消 + 批量删除（需确认） |
| 单条删除 | ✕ 按钮，需确认 |
| 内联编辑 | ✎ 按钮 → 输入框 → 保存/取消 |
| 新增 | 手动添加记忆条目 |
| 清空 | drop + recreate collection |
| 时间戳 | 精确到秒 |
| 类型标签 | 彩色标签（绿/蓝/橙/青/紫），统一首字母大写 |

**Session 管理 (MongoDB)**

- 会话列表（消息数 + token 统计）
- 查看会话详情（消息历史 + 图片 + inner_voice）
- 单条/批量/全部删除

**Message Archive (MongoDB)**

功能与 LTM 面板对齐：
- 搜索 + role 过滤（服务端分页）
- 分页（50/100/200 + 跳页）
- 复选框 + 批量删除
- 单条编辑/删除（均需确认）
- 清空全部

### 任务

TaskSystem 实时监控：
- SSE 连接（`/debug/api/tasks/stream`），自动重连
- 活跃任务卡片（状态 + 进度条 + vis-network DAG 图）
- 已完成/失败任务历史
- 节点状态颜色实时更新

## Prompt 查看器

点击概览页 "📝 LLM Prompt" 面板的 "查看" 按钮，弹出模态窗：

- **左侧**：消息列表（System Prompt / User / Assistant）— 可折叠卡片，每张可复制
- **右侧**：12 个 Section 模板 — 可折叠卡片（默认展开第一个），单 section 复制 + 全部复制
- **底部**：Tools 列表 — 两种复制格式（列表 / JSON）
- 支持 ◀ ▶ 导航浏览历史 prompt

## Raw Response 查看器

点击 "📤 LLM Raw Response" 面板的 "查看" 按钮：

- 显示 inner_voice 内容
- 显示完整原始 LLM 响应文本
- 支持 ◀ ▶ 导航

## API 端点

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/debug/api` | DebugInfo 完整 JSON |
| GET | `/debug/api/prompts/{index}` | Prompt 详情（消息 + 模板 sections） |
| GET | `/debug/api/responses/{index}` | Raw Response 详情 |
| GET | `/debug/api/db-status` | 数据库连接状态 |
| GET | `/debug/api/ltm` | LTM 列表（分页 + 搜索 + 类型过滤） |
| POST | `/debug/api/ltm` | 新增 LTM 条目 |
| PUT | `/debug/api/ltm/{id}` | 编辑 LTM 文本 |
| DELETE | `/debug/api/ltm/{id}` | 删除单条 LTM |
| DELETE | `/debug/api/ltm` | 清空 LTM |
| GET | `/debug/api/sessions` | Session 列表 |
| GET | `/debug/api/sessions/{id}` | Session 详情 |
| DELETE | `/debug/api/sessions/{id}` | 删除 Session |
| GET | `/debug/api/archive` | Archive 列表（offset 分页 + 搜索 + role 过滤） |
| PUT | `/debug/api/archive/{id}` | 编辑 Archive 消息 |
| DELETE | `/debug/api/archive/{id}` | 删除单条 Archive |
| DELETE | `/debug/api/archive` | 清空 Archive |
| GET | `/debug/api/tasks/stream` | TaskSystem SSE 事件流 |

## 技术实现

- 单文件 `static/debug.html`（~1700 行），无构建步骤
- ES5 兼容 JavaScript（无模块、无 async/await 以外的新语法）
- 轮询 + 指数退避（2s → 30s）
- DOM diff 跳过无变化轮次
- 确认弹窗复用单个 modal 元素
- Tab 切换时 SSE 自动断开
