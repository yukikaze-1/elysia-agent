# core/README.md + README 结构修正 — 设计 spec

## 背景

根目录 README.md 的项目结构树有 8 处以上过时/错误（缺文件、错文件名、测试数不对、pytest.ini 注释过期）。core/ 层缺少总览 README，文档导航里 core 层只有子模块链接。

## 目标

1. **新建 `core/README.md`** — 详细版（~300 行），覆盖 core/ 全部 28 个文件的职责、架构、数据流、扩展指南
2. **修正 README.md 项目结构树** — 对齐实际文件系统

## core/README.md 内容规划

### 1. 架构概览

三层定位图：

```
L0  Module/    无状态原子能力（LLM/STT/TTS/Embedding/DB/...）
     ↑ 依赖
L1  core/      认知中枢 — Agent → FSM → CognitiveLoop → PromptAssembler
     ↑ 使用
L2  core/memory/  记忆子系统 — SessionMemory / LongTermMemory / MessageArchive
L2  core/tools/   工具插件 — filesystem / web / memory / time / ...
L2  core/personality/  性格引擎 — PersonalityEngine / CharacterReflector
```

核心组件关系图（ASCII）：

```
EventBus ← 用户输入/系统事件
    ↓
AgentFSM (IDLE → THINKING → EXECUTING → SPEAKING → IDLE)
    ↓
CognitiveLoop (ReAct 调度 + inner_voice 状态机)
    ├── PromptAssembler (12 PromptSection → SystemPromptManager)
    ├── EmotionEngine (关键词 + STT → mood → expression)
    ├── MemoryConsolidator (对话 → LLM 提取 → LTM)
    ├── MemoryReflector (定期 LTM 去重)
    ├── TaskScheduler (Worker Pool → 工具执行)
    └── TTSStreamManager (分句 → 合成 → 播放)
```

启动顺序：

```
Agent.__init__()
  ├── CharacterConfig.load()
  ├── AppConfig (ConfigLoader)
  ├── SessionMemory / LongTermMemory
  ├── PersonalityEngine / EmotionEngine
  ├── PromptAssembler (12 sections)
  ├── ToolManager (plugin loading)
  ├── TaskScheduler
  ├── IdleMonitor
  └── OutputManager
Agent.start()
  ├── DB connect (MongoDB + Milvus, parallel)
  ├── Embedding preload (background, 10s timeout)
  ├── ToolManager.load_all()
  ├── IdleMonitor.start()
  ├── TaskScheduler.start()
  └── EventBus loop start
```

### 2. 核心组件（每个 5-10 行）

#### Agent (`agent.py`)
组装入口。`__init__` 创建所有组件，`start()` 连接数据库并启动后台任务，`stop()` 按序清理。持有 FSM、CognitiveLoop、所有子系统引用。

#### AgentFSM (`agent_fsm.py`)
有限状态机：`IDLE → THINKING → (EXECUTING → THINKING)* → SPEAKING → IDLE`。`interrupt()` 从 THINKING/SPEAKING 跳到 INTERRUPTED，只允许迁到 IDLE。同状态迁移幂等（idle→idle no-op）。

#### EventBus (`event_bus.py`)
优先级事件总线：INTERRUPT > STATE_CHANGE > NORMAL。`post()` 投递，`next()` 阻塞取，`drain()` 排空。事件携带 `data: dict`，含 trace_id 用于链路追踪。

#### CognitiveLoop (`cognitive_loop.py`)
ReAct 调度器 + inner_voice 状态机。流式 LLM 输出中解析 `</inner_voice>` 标签：PRE→THINK→REPLY 三阶段。THINK 内容发 `think` WS 事件（前端淡色气泡），REPLY 内容走分句→TTS 管线。

关键方法：
- `process(message)` — 入口，检查 FSM 状态后启动 cognitive task
- `_stream_and_dispatch()` — 流式循环：feed sentence buffer → TTS → output
- `_handle_interrupt()` — cancel 当前 task，回到 IDLE

#### PersonalityEngine (`personality/engine.py`)
四层性格模型：
- **L1 Core**: 文件加载的 base_personality（不可变）
- **L2 Self**: session 持久化的长期自我认知，Phase 2 压缩
- **L3 State**: CharacterReflector 驱动的短期自我认知
- **L4 Mood**: EmotionEngine 每轮更新

方法：`render_c1()` → C1 模板数据，`render_c9()` → C9 自我感知数据，`consume_trigger()` 接收 Reflector 事件。

#### EmotionEngine (`emotion_engine.py`)
关键词检测 + STT 情绪 → mood 映射。8 种 mood（happy/excited/sad/curious/warm/concerned/calming/neutral），intensity 衰减。`analyze_user()` 每轮调用，`analyze_user_stt()` 声学情绪路径。

#### MemoryConsolidator (`consolidator.py`)
缓冲 6 条消息 → LLM 提取 Fact/Preference/Event/Plan → `ConsolidationCompare.j2` 比对已有记忆 → insert/update/merge/skip → Milvus。失败回退全 insert。

#### MemoryReflector (`memory/memory_reflector.py`)
定期 LTM 去重：双条件触发（50 轮 + 30 条新记忆）→ KMeans 聚类 → LLM 批量审查 → merge/update/delete。

#### CharacterReflector (`personality/reflector.py`)
性格自我反思：缓冲 50 对 (user_msg, assistant_reply) → LLM 反思 → `PersonalityEngine.consume_trigger("reflector", ...)` → 更新 L3 State。

#### SessionMemory (`memory/session.py`)
对话窗口：`deque` 实现 `max_messages` 上限，MongoDB 持久化（30d TTL），`save()`/`load()` + `snapshot()`/`restore()`。`set_param()`/`get_param()` 管理运行时状态（mood、mood_intensity、awareness_cooldown 等）。

#### LongTermMemory (`memory/long_term.py`)
Milvus 向量搜索 + 语义匹配。`insert()` 自动 embedding + vector store，`search()` ANN + 字符重叠 fallback + rerank（0.5×sim + 0.3×poi + 0.2×rec）。`_absorbed_ids` 追踪 merge 吸收条目，`update_text()` 支持内容/poignancy/type 更新。

#### MessageArchive (`memory/message_archive.py`)
追加式消息归档（MongoDB `message_archive` 集合），session 视图的权威数据源。`query_by_session()` 分页查询，`count_by_session()` 统计。

#### PromptAssembler (`prompt_assembler.py`)
胶水层：创建 12 个 PromptSection → `SystemPromptManager` → `MessageBuilder`。`build_messages()` 返回最终 `messages[]` 给 LLM。`_refresh_personality_data()` 从 PersonalityEngine 拉取 C1/C9 数据。

#### IdleMonitor (`idle_monitor.py`)
空闲监控 + 主动触发。`touch()` 重置计时器，`_loop()` 检查 silence_timeout / time_of_day 触发条件。唤醒词检测待集成（openWakeWord 模型已就位）。

#### TaskScheduler (`task_scheduler.py`)
Worker Pool（N=5），`dispatch_tool_calls()` 并行执行工具。retry（2 次，指数退避），timeout（60s），INTERRUPT 时级联 cancel。`_prune_loop()` 定期清理终态 task。

#### 其他文件

| 文件 | 职责 |
|------|------|
| `llm_adapter.py` | LLM 适配器：stream_think + 重试（429/500/502/503/504，指数退避 3 次） |
| `character_config.py` | 角色配置：YAML 加载 + L1 Core 文本加载 + 路径解析 |
| `app_config.py` | 应用配置：ConfigLoader 加载 config.yaml + env override |
| `sentence_buffer.py` | 分句缓冲：逐 token feed，完整句触发 TTS |
| `tts_stream.py` | TTS 流管理：句队列 + 顺序播放 + emoji 排队 |
| `block_filter.py` | 结构化块过滤（code/markdown） |
| `text_utils.py` | 文本提取 + MessageObj |
| `message_id.py` | 消息 ID 生成（UUID4） |
| `consolidator.py` | 见 MemoryConsolidator |
| `emotion_engine.py` | 见 EmotionEngine |
| `event_bus.py` | 见 EventBus |
| `agent_fsm.py` | 见 AgentFSM |
| `debug_info.py` | /debug 面板数据聚合：collect() 遍历所有子系统 |
| `log_setup.py` | 日志系统：stderr + log/app.log + log/conversation.log + 库日志分离 |
| `timing.py` | Timer context manager + TimingClock 请求级聚合 |
| `token_tracker.py` | Token 消耗追踪 + 统计 |
| `prompt_recorder.py` | LLM Prompt 记录器（环形缓冲） |
| `response_recorder.py` | LLM Response 记录器（环形缓冲） |
| `custom_providers.py` | 自定义 provider 端点管理 |
| `tool_call_parser.py` | 流式 tool_call JSON 合并 + 规范化 |
| `task.py` | Task 数据模型：dataclass + Future + retry + 序列化 |
| `schema.py` | 共享类型（deprecated，保留参考） |
| `__init__.py` | 包标记 |

### 3. 数据流

**对话流:**
```
WebSocket text → EventBus.post(USER_MESSAGE) → Agent._process_events()
  → FSM.transition(THINKING) → CognitiveLoop.process()
    → PromptAssembler.build_messages() → LLM.stream_think()
      → inner_voice 分流 → 分句 → TTS → Output.broadcast()
        → FSM.transition(IDLE)
```

**记忆流:**
```
对话 → MemoryConsolidator.buffer(message_pair)
  → buffer 满 6 条 → LLM 提取 → ConsolidationCompare 比对
    → insert/update/skip → LongTermMemory
      → MemoryReflector.check_and_reflect()
        → KMeans 聚类 → LLM 去重 → merge/delete
```

**性格演化流:**
```
对话 → CharacterReflector.buffer(user, assistant)
  → buffer 满 50 对 → LLM 反思 → PersonalityEngine.consume_trigger()
    → L3 State 更新 → C1/C9 模板数据刷新 → PromptAssembler._refresh_personality_data()
```

### 4. 扩展指南

**添加新工具:**
```
core/tools/<name>/
├── __init__.py
├── plugin.py      # ToolPlugin 子类，定义 execute()
├── config.yaml    # SafetyProfile 配置
└── README.md
```
在 `core/tools/__init__.py` 的 `ALL_PLUGINS` 中注册。

**添加新 Prompt Section:**
1. `Module/system_prompt/templates/<section>.j2` — 模板
2. `Module/system_prompt/sections/<section>.py` — PromptSection 子类
3. `core/prompt_assembler.py` — 加到 sections 列表

**添加新记忆类型:**
- `MemoryEntry.memory_type` 枚举扩展
- `consolidator.py` 的 EXTRACTION_PROMPT 中加示例
- `long_term.py` 的 `search()` 和 rerank 逻辑按需调整

**生命周期钩子:**
- `Agent.__init__()` — 创建组件
- `Agent.start()` — 连接 DB / 启动后台任务
- `Agent._start_persistence()` — 恢复状态（`restore_state`）
- `Agent.stop()` — flush → cancel → close → save
- Component 级: `restore_state()` / `flush()` / `check_and_reflect()`

## README.md 结构树修正清单

### 修正项

| # | 位置（行） | 问题 | 修正 |
|---|-----------|------|------|
| 1 | 261 | `pytest.ini` 注释 "integration 标记" | → "pytest 配置（4 层 markers）" |
| 2 | 260 | `Makefile` 注释 "测试/检查命令" | → "测试/检查/启动命令" |
| 3 | 282 | `Module/tool/` 测试数 172 | → 129 |
| 4 | 285 | `Module/task_system/` 测试数 334 | → 341 |
| 5 | 295 | `core/tag_parser.py`（不存在） | → `core/tool_call_parser.py` |
| 6 | 289-315 | core/ 缺 5 文件 | 补: `__init__.py`, `custom_providers.py`, `task.py`, `tts_stream.py`, `tool_call_parser.py`, `response_recorder.py` |
| 7 | 315 | `core/tests/` 测试数 ~367 | → 363 |
| 8 | 317-318 | `prompt/` 只有 1 个 .j2 | → 展开为 10 个 .j2 + ref/ + README.md |
| 9 | 327-333 | `docs/` 缺文件 | 补: testing.md, database-storage.md, sync-lessons.md, experience.md, readme-conventions.md, plans/, specs/ |
| 10 | 320-325 | `static/` 缺 debug.md | 补上 |
| 11 | — | `character/` 目录缺失 | 补上 |
| 12 | — | `data/` 缺失 | 补上关键子目录 |
| 13 | — | `Module/task_system/tests/` 缺失 | 补上 |
| 14 | — | `Module/` 缺 `_common/` 的 tests 子目录 | 补上 |
| 15 | — | `core/tools/` 缺 vision 子目录 | 补上 |
| 16 | — | `.superpowers/` | 不显示（开发工具内部） |

### 预期 diff 规模

README.md: ~50 行变更（删除过时 + 新增实际）

## 产出

1. **新建** `core/README.md` — ~300 行
2. **修改** `README.md` — 项目结构树 + 文档导航补 core/README.md 链接

## 验证

```bash
# 确认 core/ 下每个 .py 文件都在 README 中有对应条目
find core/ -maxdepth 1 -name "*.py" | while read f; do
    grep -q "$(basename $f)" core/README.md || echo "MISSING: $f"
done
```
