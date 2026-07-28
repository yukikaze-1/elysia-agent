# Debug — 调试仪表板数据层

> 文档导航：[项目文档](../../README.md)

为 `/debug` 仪表板提供数据聚合和 LLM 交互录制。16 面板覆盖所有子系统状态，通过 SSE/WebSocket 实时推送到前端。

## 架构

```
┌────────────────────────────────────────────────────┐
│ DebugInfo dataclass (16 panels)                    │
│                                                    │
│ System tab:                                        │
│   FSM (state/previous) | mood | idle | run_state   │
│ Memory tab:                                        │
│   Session (msg count) | LTM (count) | DB health    │
│ Performance tab:                                   │
│   Latency (history) | Process Memory | Token       │
│ Scheduler tab:                                     │
│   Worker pool | Checkpoint                        │
│ LLM tab:                                           │
│   Prompt (ring buffer 20) | Response (ring buffer 20)│
│ Consolidation tab:                                 │
│   Buffer | History | Threshold                    │
│ Reflector tabs:                                    │
│   MemoryReflector | CharacterReflector | CharacterEngine │
│ Tools tab:                                         │
│   Tool history | submit_task history              │
│ Database tab:                                      │
│   LTM CRUD | Session management | Message archive  │
│ Tasks tab:                                         │
│   TaskSystem DAG (SSE real-time)                  │
└────────────────────────────────────────────────────┘
```

## 数据流

```
Agent._on_timer()
  → collect(agent) → DebugInfo dataclass
    → asdict(info) → JSON
      → GET /debug/api → 前端 2s 轮询渲染

LLM 调用:
  → PromptRecorder.record()   — 捕获 messages[] + tools 定义
  → ResponseRecorder.record() — 捕获原始回复 + inner_voice

Prompt/Response 查看:
  → GET /debug/api/prompts/{n}    → 最近 N 条 prompt
  → GET /debug/api/responses/{n}  → 最近 N 条 response

TaskSystem 实时推送:
  → GET /debug/api/tasks/stream → SSE → 前端 DAG 可视化
```

## 关键机制

### DebugInfo.collect()

`collect(agent)` 遍历 Agent 的 15+ 个属性，聚合为 `DebugInfo` dataclass：
- FSM 状态 → `info.fsm_state` / `info.fsm_previous`
- Cognitive → `info.current_task` / `info.latency_history`
- Memory → `info.session_message_count` / `info.ltm_entry_count`
- Token → `info.token_turn_total` / `info.token_session_total`
- Scheduler → `info.scheduler_pending_tasks` / `info.scheduler_pool_size`
- Consolidator → `info.consolidator_buffer_size` / `info.consolidator_threshold`
- Reflectors → `info.mreflector_*` / `info.creflector_*` / `info.character_engine_*`

### PromptRecorder / ResponseRecorder

环形缓冲区（容量 20），记录每次 LLM 调用的完整上下文：
- `PromptRecorder`: 完整 `messages[]` 数组 + `tools` 定义 + `request_id`
- `ResponseRecorder`: LLM 原始回复文本 + `inner_voice` 内容 + `request_id`
- 派生字段（token 数、字符数）在 `record()` 时自动计算

### API 端点

| 端点 | 说明 |
|------|------|
| `GET /debug/api` | 全量 DebugInfo JSON |
| `GET /debug/api/prompts/{n}` | 最近 N 条 LLM prompt（默认 5） |
| `GET /debug/api/responses/{n}` | 最近 N 条 LLM response（默认 5） |
| `GET /debug/api/db-status` | MongoDB + Milvus 连接状态 |
| `GET /debug/api/ltm` | LTM 条目查询（支持 filter + limit） |
| `GET /debug/api/sessions` | Session 列表 |
| `GET /debug/api/archive` | 消息归档查询 |
| `GET /debug/api/tasks/stream` | TaskSystem DAG SSE 实时流 |

## 文件布局

```
core/debug/
├── __init__.py        — 重导出 DebugInfo, collect, PromptRecorder, ResponseRecorder
├── info.py            — DebugInfo dataclass + collect() 聚合函数
├── prompt_recorder.py — PromptRecorder: LLM Prompt 环形缓冲区
├── response_recorder.py — ResponseRecorder: LLM Response 环形缓冲区
└── tests/
    └── test_prompt_recorder.py

前端:
static/
├── debug.html         — Debug 仪表板（12+ 面板，2s 轮询）
└── js/                — 面板渲染 + SSE

服务端:
core/input/server.py — /debug/api 路由注册
```

## 调试

访问 `http://localhost:8765/debug` — 仪表板入口。每个面板右上角有刷新按钮，数据 2s 自动轮询。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [core/README.md](../README.md) | Core 层总览 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 |
| [static/debug.md](../../static/debug.md) | 前端 Debug 面板说明 |
| [core/agent/README.md](../agent/README.md) | Agent — debug 数据的所有者 |
