# 全项目数据结构普查

> 生成日期: 2026-07-15 | 用途: 统一 schema 规划参考

---

## 目录

- [1. 核心类型 (core/)](#1-核心类型-core)
- [2. 消息和媒体格式 (core/schema/)](#2-消息和媒体格式-coreschema)
- [3. 工具系统 (Module/tool/)](#3-工具系统-moduletool)
- [4. TaskSystem 类型 (Module/task_system/schema/)](#4-tasksystem-类型-moduletask_systemschema)
- [5. 后端协议 ABC (Module/*/protocol.py)](#5-后端协议-abc-moduleprotocolpy)
- [6. Session Params（裸 dict，无 schema）](#6-session-params裸-dict无-schema)
- [7. WebSocket 消息格式（裸 dict）](#7-websocket-消息格式裸-dict)
- [8. 数据库文档结构（隐式）](#8-数据库文档结构隐式)
- [9. YAML 配置结构](#9-yaml-配置结构)
- [10. 常量定义](#10-常量定义)
- [11. 统一建议（按优先级）](#11-统一建议按优先级)

---

## 1. 核心类型 (core/)

### 枚举

| 文件 | 名称 | 成员 | 用途 |
|------|------|------|------|
| `core/schema/agent.py` | `AgentState` (StrEnum) | IDLE, THINKING, EXECUTING, SPEAKING, INTERRUPTED, IDLE_BACKGROUND_TASK | Agent 认知流水线状态 |
| `core/schema/agent.py` | `EventPriority` (IntEnum) | INTERRUPT=0, STATE_CHANGE=1, NORMAL=2 | 事件总线优先级 |
| `core/schema/agent.py` | `EventType` (StrEnum) | INTERRUPT, STATE_CHANGE, USER_INPUT, SYSTEM_TICK, TASK_RESULT | 事件类型 |
| `core/task/task.py:18` | `TaskType` (Enum) | TOOL_CALL, BACKGROUND | 任务分类 |
| `core/task/task.py:23` | `TaskStatus` (Enum) | PENDING, DISPATCHED, COMPLETED, FAILED, CANCELLED | 任务生命周期 |

### Dataclass

| 文件 | 名称 | 关键字段 | 用途 |
|------|------|----------|------|
| `core/monitoring/event_bus.py:15` | `Event` | priority, type, data, seq | 优先级队列事件 |
| `core/adapter/llm_adapter.py:23` | `ThoughtResult` | text: str, tool_calls: list[dict] | LLM 返回结构 |
| `core/memory/long_term.py:22` | `MemoryEntry` | id, text, embedding, memory_type, poignancy, timestamp, metadata | 长期记忆条目 |
| `core/monitoring/token_tracker.py:25` | `TokenUsage` | model, prompt_tokens, completion_tokens, total_tokens, round_num, timestamp | Token 消耗记录 |
| `core/task/task.py:37` | `Task` | id, type, status, priority, parent_id, depends_on, children_ids, payload, result, error... | 统一任务单元 |
| `core/text/text_utils.py:21` | `MessageObj` | role, content | 轻量 assistant 消息 |
| `core/debug/prompt_recorder.py:8` | `PromptRecord` | index, timestamp, round_num, model, messages, tools | 调试用 prompt 快照 |
| `core/debug/response_recorder.py:9` | `ResponseRecord` | index, timestamp, round_num, raw_text, inner_voice | 调试用 response 快照 |
| `core/text/block_filter.py:40` | `ParsedBlock` | kind, content, language | 解析后的代码/Markdown 块 |
| `core/cognitive/timing.py:24` | `Timer` | _label, _t0, _elapsed_ms | 计时上下文管理器 |
| `core/cognitive/timing.py:50` | `TimingClock` | _spans, _counts, _t_total | 请求级计时聚合 |
| `core/debug/info.py:14` | `DebugInfo` | 60+ 字段（fsm_state, session_*, lt_*, reflector_*, personality_*...） | /debug 面板聚合数据 |

### 配置 Dataclass

| 文件 | 名称 | 关键字段 | 用途 |
|------|------|----------|------|
| `core/config/app_config.py:8` | `AgentConfig` | character, user_name, max_pending_messages | Agent 级配置 |
| `core/config/app_config.py:15` | `LLMConfig` | model, base_url, api_key | LLM 后端 |
| `core/config/app_config.py:22` | `MemoryConfig` | session_max_messages, ltm_max_entries, ltm_max_age_days, ltm_embed_timeout | 记忆子系统 |
| `core/config/app_config.py:30` | `ServerConfig` | host, port, image_max_mb, image_allowed_types | 服务器 |
| `core/config/app_config.py:38` | `TTSConfig` | enabled, model, base_url, api_key, voices_config | TTS |
| `core/config/app_config.py:47` | `STTConfig` | enabled, model, base_url, api_key | STT |
| `core/config/app_config.py:56` | `WakeWordConfig` | enabled | 唤醒词 |
| `core/config/app_config.py:61` | `Live2DConfig` | static_image | Live2D |
| `core/config/app_config.py:67` | `VisionConfig` | model | 图片理解模型 |
| `core/config/app_config.py:97` | `AppConfig` | agent, llm, memory, server, tts, stt, wake_word, live2d, vision, database | **顶层配置** |
| `core/config/character_config.py:10` | `EmotionConfig` | thresholds, idle_expressions | 情绪阈值配置 |
| `core/config/character_config.py:16` | `ProactiveConfig` | enabled, interval, triggers | 主动搭话配置 |
| `core/config/character_config.py:23` | `CharacterConfig` | name, voice, user_pronoun, base_personality_file, base_personality, emotion, wake_words, proactive | **角色配置** |

### Pydantic BaseModel（工具参数）

| 文件 | 名称 | 用途 |
|------|------|------|
| `core/tools/filesystem/plugin.py` | `ReadFileParams`, `ListDirParams`, `WriteFileParams` | 文件系统工具参数 |
| `core/tools/memory/plugin.py` | `SearchMemoryParams`, `SaveMemoryParams` | 记忆工具参数 |
| `core/tools/reminder/plugin.py` | `ReminderParams` | 提醒工具参数 |
| `core/tools/runtime_config/plugin.py` | `RuntimeConfigParams` | 运行时配置工具参数 |
| `core/tools/time/plugin.py` | `TimeParams` | 时间工具参数 |
| `core/tools/vision/plugin.py` | `DescribeImageParams` | 图片描述工具参数 |
| `core/tools/web/plugin.py` | `WebSearchParams`, `WebFetchParams` | 网页工具参数 |

---

## 2. 消息和媒体格式 (core/schema/)

> 原 `Module/schema/` 已废弃并整合到 `core/schema/`（message.py, part.py, event.py, types.py 等）。

| 文件 | 名称 | 类型 | 用途 |
|------|------|------|------|
| `core/schema/part.py` | `TextPart` | Pydantic | 文本消息片段 |
| `core/schema/part.py` | `ImagePart` | Pydantic | 图片消息片段 |
| `core/schema/part.py` | `FilePart` | Pydantic | 文件消息片段 |
| `core/schema/part.py` | `AudioPart` | Pydantic | 音频消息片段 |
| `core/schema/part.py` | `VideoPart` | Pydantic | 视频消息片段 |
| `core/schema/message.py` | `UserMessage` / `AssistantMessage` | Pydantic | 聊天消息（role + content: list[Part]） |
| `core/schema/part.py` | `AudioData` / `VideoData` / `ImageData` / `FileData` | — | 原媒体数据类，已合并到 Part 类型中 |
| `core/schema/event.py` | `EventData` (Unioned) | Pydantic | 统一事件数据（Interrupt/StateChange/UserInput/SystemTick/TaskResult） |
| `core/schema/event.py` | `EventType` | StrEnum | INTERRUPT/STATE_CHANGE/USER_INPUT/SYSTEM_TICK/TASK_RESULT |
| `core/schema/message.py` | `ClientUserMessage` / `ClientAssistantMessage` / `ClientToolMessage` | TypedDict | 客户端消息类型 |
| `core/schema/types.py` | `L0InputSourceType` 等 | — | 原 io_models.py 类型，已整合或移除 |
| `core/schema/error_type.py` | `ErrorType` | StrEnum | 统一错误类型枚举（替代原 constants.py 中 DEFAULT_ERROR_* 常量） |

---

## 3. 工具系统 (Module/tool/)

| 文件 | 名称 | 类型 | 用途 |
|------|------|------|------|
| `protocol.py` | `ToolBackend` | ABC | 工具后端接口 |
| `protocol.py` | `ToolExecutor` | Protocol | 工具执行器协议 |
| `protocol.py` | `Capability` | StrEnum | 工具能力标签 |
| `protocol.py` | `ToolParameter` | dataclass | 工具参数定义（含 generate_json_schema） |
| `protocol.py` | `ToolDefinition` | dataclass | 工具定义（name + description + parameters） |
| `protocol.py` | `ErrorType` | StrEnum | INVALID_ARGUMENT/TIMEOUT/PERMISSION_DENIED/RATE_LIMITED/NETWORK_ERROR/UNAVAILABLE/CANCELLED/TOOL_ERROR/UNKNOWN_TOOL |
| `protocol.py` | `MediaItem` | dataclass | 工具返回的媒体（data/mime_type/alt_text） |
| `protocol.py` | `ToolResult` | dataclass | 工具执行结果（success/content/error/llm_content/images...） |
| `protocol.py` | `PluginContext` | dataclass | 插件上下文（session_id/memory/scheduler/config） |
| `protocol.py` | `ToolContext` | dataclass | 工具执行上下文（session_id/user_id/services） |
| `protocol.py` | `ToolProgress` | dataclass | 工具进度更新 |
| `protocol.py` | `ToolEmitter` | dataclass | 工具事件发射器 |
| `safety.py` | `SafetyProfile` | dataclass | 安全配置（网络/文件系统/数据库权限、速率限制） |

---

## 4. TaskSystem 类型 (Module/task_system/schema/)

### 基础类型 (`types.py`)

**枚举 (12 个):**

| 名称 | 成员数 | 用途 |
|------|--------|------|
| `TaskStatus` | 11 (PENDING→PLANNING→RUNNING→PAUSED→CANCELLED→COMPLETED...) | 任务 FSM |
| `SubmitRejectionCode` | 5 (NOT_READY/INVALID_PARAM/PLAN_TOO_LARGE...) | 提交拒绝码 |
| `TaskErrorCode` | 10 (PLANNING_FAILED/TOOL_ERROR/PERMISSION_DENIED...) | 任务错误码 |
| `ResumeErrorCode` | 6 (CHECKPOINT_NOT_FOUND/CORRUPTED/VERSION_MISMATCH...) | 恢复错误码 |
| `NodeStatus` | 8 (PENDING/RUNNING/SUCCESS/FAILED/SKIPPED...) | DAG 节点状态 |
| `NodeErrorCode` | 8 (TOOL_ERROR/TIMEOUT/NOT_FOUND/PERMISSION_DENIED...) | 节点错误码 |
| `RBACLevel` | 3 (READ_ONLY/SAFE_WRITE/MUTATION) | 工具权限等级 |

**NewType 别名 (6 个):** `TaskId`, `AgentId`, `ApprovalId`, `SessionId`, `NodeId`, `ArtifactId`

### 跨层协议 (`protocol.py`)

**Pydantic BaseModel (12 个):**

| 名称 | 用途 |
|------|------|
| `ConstraintConfig` | 全局约束（max_tokens, max_nodes, max_duration...） |
| `TaskConstraints` | 任务级约束（可 override 全局） |
| `UserProfile` | 用户画像（preferences, language, display_name） |
| `Tool` | 轻量工具描述符（name, description, capabilities） |
| `DelegationContext` | 委托沙箱上下文 |
| `DelegationResult` | 委托准备结果 |
| `SubmitError` | 提交拒绝错误 |
| `ResumeError` | 恢复失败错误 |
| `HITLData` | HITL 审批请求数据 |

**枚举 (3 个):** `ReturnMode` (NATURAL_LANGUAGE/STRUCTURED), `DelegationStatus` (OK/NO_MATCHING_TOOLS/TOO_VAGUE), `ResumeErrorCode`

### 查询视图 (`output.py`)

**Pydantic BaseModel (14 个):**

| 名称 | 用途 |
|------|------|
| `TaskEvent` | 任务邮箱事件（PROGRESS/APPROVAL_NEEDED/COMPLETED/FAILED...） |
| `TaskSummary` | 任务完成摘要（给 Debug WebUI） |
| `TaskProgress` | 任务进度快照 |
| `DAGNodeInfo` | DAG 节点展示信息 |
| `TaskDAG` | DAG 可视化数据 |
| `AgentInfo` / `AgentList` | Agent 运行时信息 |
| `TokenComponent` / `TokenUsage` / `TokenStats` | Token 统计（**与 core/monitoring/token_tracker.py 的 TokenUsage 同名不同义**） |

### 编排内部模型 (`internal.py`)

**Pydantic BaseModel (20+ 个):**

| 名称 | 用途 |
|------|------|
| `OrchestratorResult` | 编排器运行结果 |
| `NodeState` / `TaskState` / `ExecutionState` | 运行时状态 |
| `TaskCheckpoint` | 任务检查点（TaskState + ExecutionState） |
| `Plan` / `DAG` / `DAGNode` | 规划输出 |
| `DAGPatch` | 增量 DAG 修改（Replan） |
| `ValidationResult` / `ValidationIssue` | Plan 校验 |
| `Artifact` | 节点输出产物 |
| `NodeResult` / `NodeResultEvaluation` | 执行结果 + 评估 |
| `TaskResult` | 任务完成结构 |
| `ExperienceRecord` | 经验记忆记录 |

> **总计：TaskSystem 独占约 40 个类型，组织较规范。不建议拆改。**

---

## 5. 后端协议 ABC (Module/*/protocol.py)

| 文件 | 名称 | 类型 | 用途 |
|------|------|------|------|
| `Module/llm/protocol.py` | `LLMBackend` | ABC | LLM 后端接口 |
| `Module/stt/protocol.py` | `STTBackend` | ABC | 语音识别后端接口 |
| `Module/tts/protocol.py` | `TTSBackend` | ABC | 语音合成后端接口 |
| `Module/embedding/protocol.py` | `EmbeddingBackend` | ABC | 嵌入模型后端接口 |
| `Module/output/protocol.py` | `OutputChannel` | ABC | 输出通道接口 |
| `Module/output/protocol.py` | `ChannelCapability` | StrEnum | TEXT_DELTA/TEXT_MESSAGE/TYPING/EXPRESSION/ACTION/AUDIO/COMMAND |
| `Module/database/vectordb/protocol.py` | `VectorDBBackend` | ABC | 向量数据库接口 |
| `Module/database/documentdb/protocol.py` | `DocumentDBBackend` | ABC | 文档数据库接口 |
| `Module/database/graphdb/protocol.py` | `GraphDBBackend` | ABC | 图数据库接口（stub） |
| `Module/system_prompt/protocol.py` | `PromptSection` | ABC | 提示词 section 基类 |
| `Module/system_prompt/protocol.py` | `StaticSection` | class | 静态文本 section |
| `Module/system_prompt/protocol.py` | `Jinja2Section` | class | Jinja2 模板 section |
| `Module/task_system/protocol.py` | `HITLNotifier` | ABC | 人工审批通知协议 |

---

## 6. Session Params（裸 dict，无 schema）

存储在 `session.params` 中，通过 `set_param(key, value)` / `get_param(key, default)` 读写。MongoDB 持久化。

| Key | 类型 | 读写位置 | 用途 |
|-----|------|----------|------|
| `mood` | str | personality/engine.py, cognitive/loop.py, personality/reflector.py | 当前情绪 (neutral/happy/sad/curious/excited/warm/concerned...) |
| `mood_intensity` | float | personality/engine.py, personality/reflector.py | 情绪强度 0.0-1.0 |
| `self_l2` | str | personality/engine.py, debug_info.py | **L2 Self — 长期自我认知** |
| `state_l3` | str | personality/engine.py, debug_info.py | **L3 State — 短期状态** |
| `state_history` | list[dict] | personality/engine.py, debug_info.py | L3 变化轨迹 [{timestamp, content}, ...] |
| `compression_last_at` | float | personality/engine.py | 上次 L3→L2 压缩时间戳 |
| `compression_last_count` | int | personality/engine.py | 上次压缩时 state_history 长度 |
| `last_awareness_time` | float | personality/engine.py, debug_info.py | C9 冷却计时 |
| `self_perception_history` | list[dict] | personality/reflector.py, debug_info.py | 自我认知历史 |
| `reflector_buffer` | list[dict] | personality/reflector.py | CharacterReflector 事件缓冲 |
| `reflector_context` | list[dict] | personality/reflector.py | CharacterReflector 上下文窗口 |
| `own_life` | str | system_prompt/sections/own_life.py | Agent 自身生活叙事 |
| `mr_round_count` | int | memory/memory_reflector.py | MemoryReflector 轮次计数 |
| `mr_new_memory_count` | int | memory/memory_reflector.py | 新增记忆计数 |
| `mr_last_summary` | dict | memory/memory_reflector.py | 上次记忆反思摘要 |

> **问题：** 无类型、无文档、无集中定义。key 拼写错误只能在运行时发现。

---

## 7. WebSocket 消息格式（裸 dict）

### Server → Client（14 种）

| type | 关键 payload | 触发时机 |
|------|-------------|----------|
| `session` | messages, token_stats | 客户端连接 |
| `capabilities` | stt, tts, llm_model, live2d_static_image | 客户端连接 |
| `delta` | content, stream_id | LLM 流式 token |
| `typing` | typing: bool | 输入中指示 |
| `expression` | expression, intensity | 情绪变化 → Live2D |
| `action` | action, params | 动作触发 (wave, nod etc.) |
| `command` | command, params | 前端指令 (tool_approval etc.) |
| `state` | state: str | FSM 状态变更 |
| `message` | role, parts, content, tool_calls?, inner_voice?, content_type?, token_usage? | 完整消息 |
| `audio` | data: base64, stream_id, seq, is_final | TTS 音频块 |
| `audio_end` | stream_id, format | TTS 流结束 |
| `token_usage` | turn, session | Token 统计 |
| `transcription` | text, emotion?, events? | STT 识别结果 |
| `wake` | {} | 唤醒词检测 |
| `switch_model_result` | service, model, ok, error? | 模型切换结果 |
| `error` | message | 错误通知 |

### Client → Server（5 种）

| type | payload | 作用 |
|------|---------|------|
| `text` | content: str \| list[dict] | 用户消息（纯文本或多模态 Part 数组） |
| `interrupt` | {} | 打断当前生成 |
| `switch_model` | service: "llm"\|"stt"\|"tts", model: str | 热切换模型 |
| `tool_approval` | request_id, approved: bool | HITL 审批 |
| `wake_toggle` | enabled: bool | 开关唤醒词检测 |

> **问题：** 无 schema 定义，前端 JS 和后端 Python 各自拼 dict，不同步容易出 bug。

---

## 8. 数据库文档结构（隐式）

### MongoDB

**数据库名：** `default`（可通过配置修改，见 `Module/database/documentdb/mongodb.py`）

#### `sessions` 集合

```python
{
    "_id": ObjectId,
    "session_id": str,          # 唯一 session 标识
    "messages": [               # deque[dict]，最多 max_messages 条
        {
            "role": str,        # "user" | "assistant" | "system" | "tool"
            "content": [{"type": "text", "content": "..."}],  # TextPart 数组
            # 可选字段（取决于 role）:
            "timestamp": float,
            "tool_calls": list[dict],
            "tool_call_id": str,
            "name": str,        # 工具名
            "success": bool,
            "error": str,
            "error_type": str,
            "duration_ms": float,
            "truncated": bool,
            "inner_voice": str,
            "content_type": dict,
            "token_usage": dict,
        }
    ],
    "params": {                 # session params dict（见第 6 节）
        "mood": "neutral",
        "mood_intensity": 0.5,
        ...
    },
    "updated_at": str           # ISO 8601
}
```

**索引：** `(session_id, 1)` unique, `(updated_at, -1)`, `(updated_at, 1)` TTL=30d

#### `message_archive` 集合

```python
{
    "_id": ObjectId,            # 用于游标分页
    "session_id": str,
    "role": str,
    "content": [{"type": "text", "content": "..."}],
    # 同 sessions.messages 的可选字段
}
```

**索引：** `_id`, `(session_id, 1) + (_id, -1)` 复合索引

#### `token_usage` 集合

```python
{
    "_id": ObjectId,
    "session_id": str,
    "model": str,               # "deepseek/deepseek-chat"
    "prompt_tokens": int,
    "completion_tokens": int,
    "total_tokens": int,
    "round_num": int,
    "timestamp": float
}
```

### Milvus

#### `long_term_memory` 集合

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INT64 (primary key, auto_id) | Milvus 内部主键 |
| `embedding` | FLOAT_VECTOR (dim=1024) | bge-large-zh-v1.5 嵌入向量 |
| `entry_id` | VARCHAR (dynamic) | 应用层条目 ID |
| `text` | VARCHAR (dynamic) | 记忆文本 |
| `memory_type` | VARCHAR (dynamic) | Fact / Preference / Event / Plan / Note |
| `poignancy` | INT64 (dynamic) | 重要性 1-10 |
| `timestamp` | FLOAT (dynamic) | Unix 时间戳 |

**索引：** IVF_FLAT, L2, nlist=1024（默认）

---

## 9. YAML 配置结构

### 文件清单

| 文件 | 加载方式 | 用途 |
|------|----------|------|
| `config.yaml` | ConfigLoader(AppConfig) | 顶层应用配置 |
| `character/elysia/config.yaml` | CharacterConfig.from_yaml() | 角色定义 |
| `Module/config/providers.yaml` | load_provider_defaults() | 各后端 provider 默认值 |
| `Module/tts/minimax_voices.yaml` | TTSFactory | MiniMax 角色→voice_id 映射 |
| `Module/tts/gpt_sovits_voices.yaml` | TTSFactory | GPT-SoVits 角色→ref_audio 映射 |
| `core/tools/*/config.yaml` | ConfigLoader(SafetyProfile) | 各工具安全配置 |

### 顶层 config.yaml 结构

```yaml
agent:
  character: character/elysia/config.yaml
  user_name: 妖梦
  max_pending_messages: 32     # 消息队列上限

llm:
  model: deepseek/deepseek-v4-flash  # <provider>/<model>

memory:
  session_max_messages: 50           # deque 容量 (LLM 上下文窗口)

server:
  host: 0.0.0.0
  port: 8765
  image_max_mb: 10
  image_allowed_types: [image/png, image/jpeg, image/gif, image/webp]

tts:
  enabled: true
  model: minimax/speech-2.8-hd

stt:
  enabled: true
  model: sensevoice/sense-voice

wake_word:
  enabled: false

live2d:
  static_image: "/static/Elysia.png"

vision:
  model: mimo/mimo-v2.5
```

### 角色 config.yaml 结构

```yaml
name: Elysia
user_pronoun: 她
voice: elysia_v2
base_personality_file: personality.md

emotion:
  thresholds:
    excitement: 0.6
    sadness: 0.3
  idle_expressions: [neutral, blink]

proactive:
  enabled: false
  interval: 300
  triggers:
    - type: silence_timeout
      duration: 30
    - type: time_of_day
      times: ["08:00", "12:00", "22:00"]

wake_words: [Elysia, 爱莉, 爱莉希雅, 妖精小姐]
```

---

## 10. 常量定义

| 文件 | 名称 | 值 | 用途 |
|------|------|-----|------|
| `core/personality/engine.py` | `AWARENESS_COOLDOWN_SECONDS` | 3600 | C9 冷却（1h） |
| `core/personality/engine.py` | `AWARENESS_MAX_HISTORY` | 10 | state_history 上限 |
| `core/personality/compression.py` | `COMPRESSION_COUNT_THRESHOLD` | 5 | L3→L2 计数触发 |
| `core/personality/compression.py` | `COMPRESSION_TIME_THRESHOLD` | 604800 | L3→L2 时间触发（7d） |
| `core/personality/compression.py` | `COMPRESSION_MIN_ENTRIES` | 3 | 首次压缩最少条目 |
| `core/personality/emotion.py` | `MOOD_KEYWORDS` | dict | 10 种 mood 的关键词 |
| — | `_STT_EMOTION_TO_MOOD` | — | 已移除：STT 情感现在直接传给 LLM 由 C5 Sensory 处理 |
| `core/schema/error_type.py` | `ErrorType` | StrEnum | 统一错误类型枚举（替代原 `Module/schema/constants.py` 中 DEFAULT_ERROR_* 常量） |
| `Module/database/vectordb/milvus.py` | `INDEX_PRESETS` | dict | 4 种向量索引预设 |

---

## 11. 统一建议（按优先级）

| 优先级 | 类别 | 现状 | 建议 |
|--------|------|------|------|
| **🔴 高** | Session params | 15 个裸 key，散落 6+ 文件，无类型 | 建立 `SessionParams` dataclass，集中定义所有 key、类型、默认值、注释 |
| **🔴 高** | WebSocket 消息 | 19 种消息类型，全是 ad-hoc dict | 定义 `WSMessage` 联合类型或 Pydantic 模型，前后端共享约束 |
| **🔴 高** | 两套 Event 定义 | 原 `core/schema.py` 和 `Module/schema/event.py` 各有一套 | 已合并到 `core/schema/event.py`（统一 EventData 联合类型） |
| **🟡 中** | MongoDB 文档 | sessions/messages/token_usage 隐式结构 | 至少给文档 dict 提供 TypedDict 或 dataclass，标注字段 |
| **🟡 中** | 消息 Part 类型 | 原 `Module/schema/message_format.py` 标记废弃 | 已迁移到 `core/schema/part.py`（Pydantic BaseModel） |
| **🟡 中** | PersonalityEngine params | self_l2/state_l3/compression_last_* 等都是裸 session param | 纳入 `SessionParams`，或独立 `PersonalityState` dataclass |
| **🟢 低** | TaskSystem schema | 40+ 类型，组织良好，在独立目录 | 保持不动 |
| **🟢 低** | 后端 ABC | 各在 protocol.py，模式统一 | 保持不动 |
| **🟢 低** | 配置 dataclass | 结构清晰，ConfigField 约束完整 | 保持不动 |

---

## 统计摘要

| 类别 | 数量 |
|------|------|
| Dataclass（纯数据类） | ~30 |
| Pydantic BaseModel | ~34 |
| Enum / StrEnum / IntEnum | ~29 |
| NewType 别名 | 6 |
| ABC（抽象基类） | ~10 |
| Protocol | 1 (ToolExecutor) |
| 裸 dict 结构（Session params） | 15 keys |
| 裸 dict 结构（WS 消息） | 19 种类型 |
| MongoDB collections | 3 |
| Milvus collections | 1 |
| YAML 配置文件 | 11+ |
| **总计** | **约 120 个数据结构定义** |
