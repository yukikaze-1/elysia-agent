# Elysia 记忆系统架构

## 1. 总览

Elysia 的记忆系统采用 **三层架构**，从短到长、从原始到抽象逐级整合：

```
对话消息 ──→ SessionState (短时记忆/工作记忆)
    │              │
    │         [L1 上下文窗口: 最近 20 条消息]
    │
    └──→ Reflector 缓冲区
              │
              │ (缓冲达到阈值: 6 条消息)
              ▼
         MicroReflector (LLM 提取情景记忆)
              │
              ▼
         Milvus micro_memory ────→ L1 检索 (向量搜索, top 5)
              │
              │ (时间间隔到达: 每天一次)
              ▼
         MacroReflector (LLM 合成日记)
              │
              ▼
         Milvus macro_memory ────→ L1 检索 (向量搜索, top 3)
```

三层记忆各有不同的生命周期和访问方式:

| 层级 | 存储位置 | 数据形式 | 生命周期 | 访问方式 |
|------|---------|---------|---------|---------|
| **短时记忆** | 内存 (SessionState) | 原始对话 `ChatMessage` | 100 条消息后裁剪 | 按时间顺序取最近 N 条 |
| **微记忆** | Milvus `micro_memory` | LLM 提取的结构化情景记忆 | 持久化（暂未实现遗忘） | 向量相似度搜索 + rerank |
| **宏记忆** | Milvus `macro_memory` | LLM 合成的每日日记 | 持久化（暂未实现遗忘） | 向量相似度搜索 + rerank |

---

## 2. 记忆类型与 Schema

### 2.1 ChatMessage（对话消息）

文件: [core/ChatMessage.py](../core/ChatMessage.py)

```python
@dataclass
class ChatMessage:
    role: str            # 发言者: 用户名 或 AI 角色名
    content: str         # 消息正文
    inner_voice: str     # 内心独白（仅 AI 消息）
    timestamp: float     # Unix 时间戳
    type: MessageType    # NORMAL / ACTIVE_SPEAK / SYSTEM
    # 多模态字段（预留）
    images: list = []
    audio: list = []
    video: list = []
    files: list = []
```

工厂方法: `from_ChatCompletionMessage()`, `from_UserMessage()`
序列化: `to_dict()`, `from_dict()`

### 2.2 MicroMemory（微记忆 / 情景记忆）

文件: [workers/reflector/MemorySchema.py](../workers/reflector/MemorySchema.py)

三层继承结构:

```
MicroMemoryLLMOut        ← LLM 直接输出的原始格式
    └── MicroMemory      ← + timestamp, 业务层使用的标准格式
        └── MicroMemoryStorage ← + embedding, Milvus 存储格式
```

字段说明:

| 字段 | 类型 | 说明 |
|------|------|------|
| `content` | `str` | 记忆正文（第一人称中文叙事） |
| `subject` | `str` | 记忆涉及的对象 |
| `memory_type` | `str` | 类型: Fact / Preference / Event / Opinion / Experience |
| `poignancy` | `int` | 重要性评分 1-10 |
| `keywords` | `list[str]` | 关键词标签 |
| `timestamp` | `float` | 发生时间（继承自 MicroMemory） |
| `embedding` | `list[float]` | 1024 维向量（继承自 MicroMemoryStorage） |

### 2.3 MacroMemory（宏记忆 / 日记条目）

文件: [workers/reflector/MemorySchema.py](../workers/reflector/MemorySchema.py)

同样三层继承:

```
MacroMemoryLLMOut        ← LLM 直接输出的原始格式
    └── MacroMemory      ← + timestamp, 业务层使用的标准格式
        └── MacroMemoryStorage ← + embedding, Milvus 存储格式
```

字段说明:

| 字段 | 类型 | 说明 |
|------|------|------|
| `diary_content` | `str` | 日记正文（第一人称中文段落） |
| `subject` | `str` | 日记描述的对象 |
| `poignancy` | `int` | 情感强度 1-100 |
| `dominant_emotion` | `str` | 主导情绪（如 "喜悦", "悲伤"） |
| `keywords` | `list[str]` | 关键词 |
| `timestamp` | `float` | 日期时间戳 |
| `embedding` | `list[float]` | 1024 维向量 |

### 2.4 辅助类型

文件: [core/Schema.py](../core/Schema.py)

- **ConversationSegment**: `(start_time, end_time, messages: list[ChatMessage])` — MicroReflector 按时间间隔切割对话时使用
- **UserMessage**: 用户输入消息的标准化格式
- **EventType 枚举**: `USER_INPUT`, `SYSTEM_TICK`, `MICRO_REFLECTION_DONE`, `MACRO_REFLECTION_DONE`, `REFLECTION_DONE`
- **EventSource 枚举**: `REFLECTOR`, `SYSTEM_CLOCK`, `L0_SENSOR` 等

---

## 3. 短时记忆（SessionState）

文件: [core/SessionState.py](../core/SessionState.py)

### 职责

维护内存中的对话历史（上下文窗口），为 L1 BrainLayer 提供最近的对话上下文。

### 核心属性

| 属性 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `conversations` | `list[ChatMessage]` | `[]` | 完整对话历史 |
| `max_messages_limit` | `int` | 100 | 最大消息数（超出后裁剪） |
| `max_inner_limit` | `int` | 5 | 保留 inner_voice 的最近消息数 |
| `last_interaction_time` | `float` | — | 最后交互时间戳 |
| `last_ai_reply_time` | `float` | — | 最后 AI 回复时间 |
| `last_user_reply_time` | `float` | — | 最后用户回复时间 |
| `last_speaker` | `str` | — | 最后发言者角色名 |

### 关键方法

```
add_messages(msgs)        → 追加消息，更新 time state，超限时触发 prune
get_recent_history(limit, inner_limit) → 取最近 N 条，清洗较早的 inner_voice
get_full_history()        → 返回完整历史
prune_history()           → 裁剪到 max_messages_limit 条
dump_state() / load_state(state) → 检查点持久化
```

### 生命周期

- 启动时由 CheckPointManager 从 `storage/runtime_state.json` 恢复
- 每个 SYSTEM_TICK 由 CheckPointManager 保存到磁盘
- 消息数超过 `session_capacity`(100) 时自动裁剪到最近 100 条

---

## 4. 长时记忆（L2 MemoryLayer + Milvus）

文件: [layers/L2/L2.py](../layers/L2/L2.py)

### 设计

`MemoryLayer` 是**线程安全的单例**（双重检查锁），统一管理 Milvus 向量数据库中的两种记忆集合。main.py 和 Reflector 后台线程共享同一实例。

### Milvus 集合

#### micro_memory 集合

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INT64 (PK, auto_id) | 主键 |
| `embedding` | FLOAT_VECTOR(1024) | 向量，IVF_FLAT 索引，L2 距离 |
| `subject` | VARCHAR(255) | 记忆对象 |
| `content` | VARCHAR(65535) | 记忆正文 |
| `memory_type` | VARCHAR(20) | Fact/Preference/Event/Opinion/Experience |
| `poignancy` | INT8 | 重要性 1-10 |
| `timestamp` | INT64 | Unix 时间戳 |
| `keywords` | ARRAY[VARCHAR(128), 50] | 关键词列表 |

#### macro_memory 集合

与 micro_memory 类似，区别在于:
- `content` → `diary_content`（日记正文）
- `memory_type` → `dominant_emotion`（主导情绪）

### 向量嵌入

- 模型: `BAAI/bge-large-en-v1.5`（通过 `HuggingFaceEmbeddings` 加载）
- 维度: **1024**
- 设备: 自动检测 CUDA / CPU
- 本地缓存: `/home/yomu/Elysia/model_cache/{model_name}`
- L2 归一化: 开启

### 核心接口

```
retrieve_context(query)         → (list[MicroMemory], list[MacroMemory])
   检索混合上下文: 5 条微记忆 + 3 条宏记忆

save_micro_memory(memories)     → 嵌入 content 字段并插入 Milvus

save_macro_memory(memories)     → 嵌入 diary_content 字段并插入 Milvus

get_recent_micro_memories(start_time, min_poignancy) → 标量查询
   被 MacroReflector 调用, 获取最近 24h 高重要性微记忆

retrieve(mem_type, query_text, top_k) → 向量 ANN 搜索 + rerank + 格式转换

query(mem_type, filter, output_fields) → 标量查询（按时间戳/重要性过滤）
```

---

## 5. 记忆检索

### 检索调用链

```
UserInputHandler._execute_brain_decision()
  │
  ├── session.get_recent_history(limit=20)     → 短时记忆 (最近 20 条对话)
  │
  └── l2.retrieve_context(query=user_input.content)
        │
        ├── retrieve('Micro', query, top_k=5)  → 微记忆向量搜索
        └── retrieve('Macro', query, top_k=3)  → 宏记忆向量搜索
              │
              └── L1.generate_reply(micro_memories=..., macro_memories=..., ...)
                    └── Brain.j2 模板渲染 → LLM 调用
```

### 向量搜索流程 (`retrieve` 方法)

1. **嵌入查询文本**: 使用 HuggingFaceEmbeddings 将 query 转为 1024 维向量
2. **ANN 搜索**: Milvus `search(anns_field="embedding", metric_type="L2", limit=20)` — 先用 IVF_FLAT 索引粗排，取 20 个候选
3. **Rerank (精排)**: 对 20 个候选按综合分数重新排序，取 top_k

### Rerank 算法

#### 微记忆评分 (`_reank_micro`)

```
similarity  = 1 / (1 + L2_distance)           # L2 距离 → 相似度 [0,1]
poignancy_score = poignancy / 10.0            # 重要性归一化 [0,1]
days_diff = (now - timestamp) / 86400
recency_score = e^(-0.1 × days_diff)          # 指数衰减, 半衰期 ≈ 6.9 天

final_score = 0.5 × similarity + 0.4 × poignancy_score + 0.1 × recency_score
```

#### 宏记忆评分 (`_rerank_macro`)

```
final_score = 0.5 × similarity + 0.35 × poignancy_score + 0.15 × recency_score
衰减系数 = 0.05 (半衰期 ≈ 13.9 天, 比微记忆更平缓)
```

**设计意图**: 相关性权重最高 (50%)，微记忆侧重重要性 (40%)，宏记忆增加时间因素的比重 (15%) 且衰减更慢，保证长期日记能被持续检索到。

### 记忆注入 LLM Prompt

检索到的记忆通过 [Brain.j2](../prompt/Brain.j2) 的 `BrainSystemPrompt` 模板注入 LLM:

```xml
<memory_bank>
  <micro memory>
    - 2024-01-15 14:30 (Poignancy:8) 我注意到小明今天似乎心情不太好...
    - 2024-01-15 10:00 (Poignancy:6) 小明告诉我他喜欢喝咖啡...
  </micro memory>
  <macro memory>
    - 2024-01-14 (Poignancy:75) (Dominant Emotion: 温暖) 今天和小明的交流让我...
  </macro memory>
</memory_bank>
```

格式化由 [Utils.j2](../prompt/Utils.j2) 中的 `FormatMicroMemories` 和 `FormatMacroMemories` 宏完成。

---

## 6. 记忆创建流水线

### 端到端数据流

```
[用户输入]
  │
  ▼
L0 SensorLayer (接收输入, 触发 Amygdala 情绪感知)
  │
  ▼
EventBus.publish(USER_INPUT event)
  │
  ▼
Dispatcher → UserInputHandler.handle()
  │
  ├── L2.retrieve_context() → 获取相关记忆
  ├── SessionState.get_recent_history() → 获取最近对话
  ├── L1.generate_reply() → 生成回复
  ├── ActuatorLayer → 输出回复
  │
  └── _save_to_memory(user_msg, ai_msg)
        │
        ├── SessionState.add_messages([user_msg, ai_msg])  → 写入短时记忆
        │
        └── Reflector.on_new_message(user_msg)              → 写入反射缓冲区
        └── Reflector.on_new_message(ai_msg)
              │
              ▼
        [Reflector 后台线程轮询]
              │
              │ 缓冲区 >= micro_threshold (6 条)
              ▼
        MicroReflector.run_micro_reflection()
              │
              ├── conversation_split() → 按 30min 间隔切割对话段
              ├── LLM 提取 → MicroMemoryLLMOut (JSON 数组)
              ├── 转为 MicroMemory (加时间戳)
              └── MemoryLayer.save_micro_memory()
                    ├── embed(content) → 1024 维向量
                    └── MilvusClient.insert(micro_memory)
```

### 主动发言路径

当 AI 主动发起对话（由 SystemTickHandler 触发）时，消息同样走 `SessionState.add_messages()` + `Reflector.on_new_message()`，与被动回复一致。

---

## 7. Reflection 系统

文件:
- [workers/reflector/Reflector.py](../workers/reflector/Reflector.py) — 编排器
- [workers/reflector/MicroReflector.py](../workers/reflector/MicroReflector.py) — 微反射
- [workers/reflector/MacroReflector.py](../workers/reflector/MacroReflector.py) — 宏反射

### 7.1 Reflector（编排器）

`Reflector` 是一个后台守护线程，负责:

```
_background_loop() 循环:
  1. sleep(worker_sleep_interval)  ← 默认 2s
  2. 检查 _should_run_micro(): 缓冲区 >= micro_threshold (默认 6)
     → _run_micro_reflection_sync() → 清空缓冲区 → 调用 MicroReflector
  3. 检查 _should_run_macro(): 距离上次 >= macro_interval_seconds (默认 120s 测试, 正式 86400s)
     → _trigger_macro_reflection() → 异步线程 → 调用 MacroReflector
```

核心状态:

| 属性 | 说明 |
|------|------|
| `buffer: list[ChatMessage]` | 待处理的对话缓冲区（线程安全） |
| `last_macro_run: datetime` | 上次宏反射时间 |
| `micro_threshold: int` | 触发微反射的缓冲阈值 |
| `macro_interval_seconds: int` | 宏反射间隔 |

检查点支持: `dump_state()` / `load_state()`，保存缓冲区和反射器状态。

### 7.2 MicroReflector（微反射）

**目的**: 从原始对话日志中提取结构化的情景记忆。

**流程**:

1. `conversation_split(conversations, gap_seconds=1800)` — 按 30 分钟无对话间隔切割对话为 `ConversationSegment`
2. 对每个段落:
   - 构建 LLM 消息（System Prompt + User Prompt）
   - 调用 DeepSeek v4-pro（temperature=1.2, max_tokens=1500）
   - 使用 prefix continuation (`"[\n"`) 确保输出 JSON 数组格式
   - 解析 JSON → `MicroMemoryLLMOut` 列表 → 转为 `MicroMemory`（加时间戳）
3. `MemoryLayer.save_micro_memory()` — 嵌入并写入 Milvus

**LLM Prompt 角色**: "Subconscious Processor"（潜意识处理器）

**提取规则**:
- 提取事实、偏好、事件、情绪
- 过滤噪音（问候、闲聊）
- 重要性评分 1-10（1-3 琐碎, 4-7 中等, 8-10 核心记忆）
- **事件合并**: 将因果/反应/结果合并为一个综合记忆节点（不拆散）
- 第一人称中文叙事（"我注意到...", "我发现..."）

### 7.3 MacroReflector（宏反射）

**目的**: 将一天的高重要性微记忆合成为日记条目，模拟"睡眠时潜意识整理白天经历"。

**流程**:

1. `_gather_daily_memories()` — 从 Milvus 查询最近 24 小时 `poignancy >= 3` 的微记忆
2. `_generate_macro_memories()`:
   - System Prompt: 角色为"睡眠中的潜意识"
   - User Prompt: 昨日日记（延续性）+ 今日高情感记忆列表
   - 调用 LLM（temperature=1.0, max_tokens=3000）
   - 使用 prefix continuation (`"{\n"`) 确保输出 JSON 对象格式
   - 解析 JSON → `MacroMemoryLLMOut` → 转为 `MacroMemory`
3. `MemoryLayer.save_macro_memory()` — 嵌入 `diary_content` 并写入 Milvus

**LLM 输出**:
- **Task 1 — 日记**: 一段第一人称中文段落，主观、感性、原始，聚焦于关系变化
- **Task 2 — 情绪分析**: poignancy (0-100 情感强度) + dominant_emotion (中文情绪标签)

---

## 8. 记忆整合（Consolidation）

Elysia 的记忆整合是**分层递进**的过程：

```
碎片对话 ──→ 情景记忆 ──→ 日记条目
(原始)      (Micro)      (Macro)
```

### 第一层整合: 对话 → 微记忆

- **触发**: 每积累 ~6 条消息（约 3 轮对话）
- **机制**: LLM 在 prompt 中被明确要求 **事件合并** — "合并具有因果/反应/结果关系的事件为一个综合记忆节点，而非拆分碎片"
- **效果**: 多轮对话中的相关片段被压缩为一个完整的叙事节点

### 第二层整合: 微记忆 → 日记

- **触发**: 每天一次（生产环境 86400s）
- **机制**: LLM 将全天高重要性微记忆蒸馏为单一日记段落 + 整体情绪评估
- **延续性**: 每次宏反射会接收昨日日记作为上下文，保持叙事连贯性

### 检索时的动态整合

- 每次检索同时获取微记忆(5 条)和宏记忆(3 条)
- 宏记忆提供"大背景"，微记忆提供"具体细节"
- Rerank 算法在检索时进一步按相似度 + 重要性 + 新鲜度做动态融合

---

## 9. 检查点与持久化

### 磁盘持久化 (JSON)

文件: [core/CheckPointManager.py](../core/CheckPointManager.py)

**保存位置**: `storage/runtime_state.json`（原子写入）

**保存时机**: 每个 SYSTEM_TICK（约每 10s）+ 关闭时

**注册的模块**:

| 模块 | getter | setter | 保存内容 |
|------|--------|--------|---------|
| BrainLayer (L1) | `get_snapshot()` | `load_snapshot()` | 模型配置, 最后思考日志 |
| PersonaLayer (L3) | `get_snapshot()` | `load_snapshot()` | mood, persona prompt |
| Reflector | `dump_state()` | `load_state()` | 缓冲区, 最后宏反射时间, 微/宏反射器状态 |
| SessionState | `dump_state()` | `load_state()` | 完整对话历史 (ChatMessage 序列化) |
| PsycheSystem | `dump_state()` | `load_state()` | energy, social_battery, boredom, mood 等 |

### Milvus 持久化

- `micro_memory` 和 `macro_memory` 集合中的向量数据由 Milvus 管理持久化
- **不经过** CheckPointManager
- 重启后直接可用（Milvus 服务独立运行）

### 启动恢复流程

1. `CheckPointManager.load_checkpoint()` → 恢复 JSON 状态
2. `MemoryLayer.__init__()` → 连接 Milvus, 检查集合是否存在
3. `Reflector.start()` → 启动后台线程开始监听

---

## 10. 配置参数

文件: [config/Config.py](../config/Config.py) + [config/config.yaml](../config/config.yaml)

### MemoryLayer (L2)

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `micro_memory_collection` | `"micro_memory"` | Milvus 微记忆集合名 |
| `macro_memory_collection` | `"macro_memory"` | Milvus 宏记忆集合名 |
| `MILVUS_URI` | `"http://localhost:19530"` | Milvus 服务地址 (环境变量覆盖) |
| `MILVUS_TOKEN` | `"root:Milvus"` | Milvus 认证令牌 (环境变量覆盖) |

### Reflector

| 参数 | config.yaml 值 | Config.py 默认值 | 说明 |
|------|---------------|-----------------|------|
| `micro_threshold` | 6 | 10 | 触发微反射的缓冲消息数 |
| `macro_interval_seconds` | 120 (开发) | 86400 (24h) | 宏反射间隔（开发/正式） |
| `worker_sleep_interval` | 2.0 | 2.0 | 后台轮询间隔(秒) |

### MicroReflector

| 参数 | 值 |
|------|-----|
| `conversation_split_gap_seconds` | 1800 (30 分钟) |
| `model` | `deepseek-v4-pro` |
| `temperature` | 1.2 |
| `max_tokens` | 1500 |

### MacroReflector

| 参数 | 值 |
|------|-----|
| `gather_memory_time_interval_seconds` | 86400 (24 小时) |
| `model` | `deepseek-v4-pro` |
| `temperature` | 1.0 |
| `max_tokens` | 3000 |

### SessionState

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `session_capacity` | 100 | 最大消息数 |
| `inner_capacity` | 5 | 保留 inner_voice 的最近消息数 |
| `persist_dir` | `storage/sessions` | 会话文件目录（已废弃，现由 CheckPointManager 管理） |

### 环境变量

| 变量 | 用途 |
|------|------|
| `DEEPSEEK_API_KEY` | LLM API 密钥 |
| `DEEPSEEK_API_BETA` | LLM API 地址 |
| `MILVUS_URI` | Milvus 连接地址 |
| `MILVUS_TOKEN` | Milvus 认证令牌 |

---

## 11. Prompt 模板

### 11.1 MicroReflector.j2

文件: [prompt/MicroReflector.j2](../prompt/MicroReflector.j2)

两个 Jinja2 宏:

- **`MicroReflectorSystemPrompt(character_name, user_name)`**: 定义 "Subconscious Processor" 角色，包含提取规则（事实/偏好/事件/情绪、过滤噪音、重要性 1-10、事件合并、中文输出、第一人称）
- **`MicroReflectorUserPrompt(recent_conversations)`**: 渲染对话日志为 role/content/inner_thought 格式

### 11.2 MacroReflector.j2

文件: [prompt/MacroReflector.j2](../prompt/MacroReflector.j2)

两个 Jinja2 宏:

- **`MacroReflectorSystemPrompt(character_name)`**: "睡眠中的潜意识处理白天经历"，输出日记段落 + 关系情绪分析
- **`MacroReflectorUserPrompt(character_name, current_date, last_diary_entry, memories_list)`**: 昨日日记 + 今日高情感记忆列表

### 11.3 Brain.j2（记忆相关宏）

文件: [prompt/Brain.j2](../prompt/Brain.j2)

- **`L2MemoryBlockPrompt(memory_ctx)`**: 将微记忆和宏记忆分别渲染到 `<micro memory>` 和 `<macro memory>` XML 标签中，带时间戳和重要性
- **`BrainSystemPrompt(l3_personality_block, sensory, memory, state)`**: 完整 L1 系统提示，包含 Dual-Track Thinking Protocol，条件性注入记忆库

### 11.4 Utils.j2（格式化宏）

文件: [prompt/Utils.j2](../prompt/Utils.j2)

- **`FormatHistory(messages)`**: `list[ChatMessage]` → 文本行
- **`FormatMicroMemories(micro_memories)`**: `list[MicroMemory]` → 带时间戳和重要性的文本
- **`FormatMacroMemories(macro_memories)`**: `list[MacroMemory]` → 带时间戳、重要性和情绪的文本

---

## 12. Module 抽象层（尚未接入主流程）

项目中有较新的模块化抽象层，代表架构演进方向，但目前**未接入**主流程。

### 向量数据库抽象

目录: [Module/database/vectordb/](../Module/database/vectordb/)

- `protocol.py` — `VectorDBBackend` 抽象基类，定义异步接口: `create_collection`, `drop_collection`, `insert`, `search`, `query`, `close`
- `milvus.py` — `MilvusBackend(VectorDBBackend)`，通过 `asyncio.to_thread()` 包装 pymilvus 同步调用，支持 IVF_FLAT 和 HNSW 索引
- `registry.py` — 后端注册表
- `config.py` — 配置 dataclass

### 嵌入模型抽象

目录: [Module/embedding/](../Module/embedding/)

- `protocol.py` — `EmbeddingBackend` 抽象基类: `embed_documents`, `embed_query`, `dimension`
- `huggingface.py` — `HuggingFaceBackend`（默认 `BAAI/bge-large-zh-v1.5`, 1024 维）
- `registry.py` — `EmbeddingFactory`，支持 `huggingface/` 和 `openai/` provider 格式
- `config.py` — `EmbeddingConfig`

**当前状态**: 主流程仍使用 `Utils.create_embedding_model()` + `pymilvus.MilvusClient` 直接调用，Module 抽象层作为未来重构方向。

---

## 13. 文件索引

| 文件 | 职责 |
|------|------|
| [layers/L2/L2.py](../layers/L2/L2.py) | **核心**: MemoryLayer 单例，Milvus CRUD，向量搜索，rerank |
| [workers/reflector/Reflector.py](../workers/reflector/Reflector.py) | Reflector 编排器 + MemoryReflector 包装器 |
| [workers/reflector/MicroReflector.py](../workers/reflector/MicroReflector.py) | 微反射: LLM 从对话提取情景记忆 |
| [workers/reflector/MacroReflector.py](../workers/reflector/MacroReflector.py) | 宏反射: LLM 从微记忆合成日记 |
| [workers/reflector/MemorySchema.py](../workers/reflector/MemorySchema.py) | MicroMemory / MacroMemory 数据结构及继承链 |
| [core/SessionState.py](../core/SessionState.py) | 短时记忆: 对话历史，裁剪，时间状态追踪 |
| [core/ChatMessage.py](../core/ChatMessage.py) | ChatMessage dataclass + 序列化 |
| [core/Schema.py](../core/Schema.py) | Event / EventType / ConversationSegment / UserMessage |
| [core/CheckPointManager.py](../core/CheckPointManager.py) | 通用检查点持久化框架（原子 JSON 写入） |
| [core/AgentContext.py](../core/AgentContext.py) | 依赖注入容器，包含所有层引用 |
| [core/handlers/UserInputHandler.py](../core/handlers/UserInputHandler.py) | 编排记忆检索 → L1 生成 → 记忆存储 |
| [core/handlers/SystemTickHandler.py](../core/handlers/SystemTickHandler.py) | 心跳: 检查点保存 + 主动发言决策 |
| [layers/L1.py](../layers/L1.py) | BrainLayer: 使用记忆上下文生成回复 |
| [layers/L3.py](../layers/L3.py) | PersonaLayer: mood + persona 注入 L1 提示 |
| [prompt/MicroReflector.j2](../prompt/MicroReflector.j2) | 微反射 LLM 提示模板 |
| [prompt/MacroReflector.j2](../prompt/MacroReflector.j2) | 宏反射 LLM 提示模板 |
| [prompt/Brain.j2](../prompt/Brain.j2) | L1 系统提示 (含记忆块注入) |
| [prompt/Utils.j2](../prompt/Utils.j2) | 共享 Jinja2 宏 (FormatHistory, FormatMicroMemories, FormatMacroMemories) |
| [Utils.py](../Utils.py) | `create_embedding_model()`, `parse_json()`, `timedelta_to_text()` |
| [config/Config.py](../config/Config.py) | 所有配置 dataclass |
| [config/config.yaml](../config/config.yaml) | 运行时配置值 |
| [operate_db.py](../operate_db.py) | CLI: 检查 / 清理 Milvus 集合 |
| [Module/database/vectordb/protocol.py](../Module/database/vectordb/protocol.py) | VectorDB 抽象基类（新模块，待接入） |
| [Module/database/vectordb/milvus.py](../Module/database/vectordb/milvus.py) | Milvus 异步后端（新模块，待接入） |
| [Module/embedding/protocol.py](../Module/embedding/protocol.py) | Embedding 抽象基类（新模块，待接入） |
| [Module/embedding/huggingface.py](../Module/embedding/huggingface.py) | HuggingFace 异步嵌入后端（新模块，待接入） |
| [main.py](../main.py) | CLI 入口，组装所有组件 |
