桌面虚拟角色 Agent 核心架构设计方案

## 1. 架构总览 (Layered Architecture)

整个系统自下而上分为三层，确保底层能力与上层认知逻辑解耦：

**L0 基础设施层 (Infrastructure)**：你已完成的 Module 目录。提供无状态的原子能力（LLM, STT, TTS, Embedding, 数据库操作）和多通道 I/O（OutputManager）。

**L1 认知控制层 (Cognitive Core)**：Agent 的"大脑"与"中枢神经"。负责事件分发、状态机流转、提示词组装、ReAct 认知循环调度、角色人格注入、唤醒/主动搭话决策。

**L2 记忆与工具层 (Memory & Skills)**：Agent 的外部挂载。包含短期上下文（MongoDB）、长期历史（Milvus）以及标准化的工具集（Tool Registry）。

```
┌─────────────────────────────────────────────────────┐
│ L2  Memory & Skills                                  │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │ MongoDB  │  │   Milvus     │  │ Tool Registry │   │
│  │ Session  │  │ Long-term    │  │ (OpenAI       │   │
│  │ Context  │  │ Memory       │  │  Tool Schema) │   │
│  └──────────┘  └──────────────┘  └──────────────┘   │
├─────────────────────────────────────────────────────┤
│ L1  Cognitive Core                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │ EventBus │  │ AgentFSM │  │ CognitiveLoop     │   │
│  │(Priority │  │          │  │ (ReAct scheduler) │   │
│  │ Queue)   │  │          │  │                   │   │
│  └──────────┘  └──────────┘  └──────────────────┘   │
│  ┌──────────────┐  ┌──────────────┐                 │
│  │ CharacterCfg │  │ IdleMonitor  │                 │
│  │ (personality,│  │ (wake word,  │                 │
│  │  emotion)    │  │  proactive)  │                 │
│  └──────────────┘  └──────────────┘                 │
│  ┌──────────────────────────────────────────────┐    │
│  │ PromptAssembler                              │    │
│  │ (Jinja2 render + memory injection)            │    │
│  └──────────────────────────────────────────────┘    │
├─────────────────────────────────────────────────────┤
│ L0  Infrastructure (Module/)                         │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌───────────┐          │
│  │ LLM  │ │ STT  │ │ TTS  │ │ Embedding │          │
│  └──────┘ └──────┘ └──────┘ └───────────┘          │
│  ┌──────────┐ ┌────────┐ ┌────────────────────┐     │
│  │ Output   │ │ Config │ │ Database           │     │
│  │ Manager  │ │ Loader │ │ (Vector/Doc/Graph) │     │
│  └──────────┘ └────────┘ └────────────────────┘     │
└─────────────────────────────────────────────────────┘
```

## 2. 核心中枢：事件总线与状态机

桌面助手的核心痛点是"随时被打断"和"多任务并行"。我们将采用基于优先级的事件驱动架构 (Event-Driven FSM)。

### 事件总线 (EventBus)

基于 `asyncio.PriorityQueue`：

| 优先级 | 类型 | 触发源 | 行为 |
|--------|------|--------|------|
| 0 | `INTERRUPT` | 用户突然说话（VAD 检测）、点击"停止"按钮 | 无条件取消当前所有推理和音频输出任务 |
| 1 | `STATE_CHANGE` | FSM 状态流转 | 推给前端切换动画（待机/思考/说话/工作） |
| 2 | `USER_INPUT` | STT 识别完成、WebSocket 文本消息 | 常规对话请求，进入认知循环 |
| 2 | `SYSTEM_TICK` | 定时器（IdleMonitor） | 触发主动搭话决策、环境感知 |

任何状态在接收到 Priority 0 中断信号时，流转至 `INTERRUPTED` 进行资源清理（`task.cancel()`），然后安全降级回 `IDLE`。

### 有限状态机 (AgentFSM)

```
                    ┌──────────────────────────────┐
                    │         INTERRUPTED           │
                    │  (cleanup active tasks)       │
                    └──────────────────────────────┘
                      ▲                         │
              Priority 0 (any state)            │ (cleanup done)
                      │                         ▼
  ┌──────┐   INPUT   ┌──────────┐   LLM    ┌──────────┐  done   ┌──────────┐
  │ IDLE │ ────────► │ THINKING │ ◄──────► │EXECUTING │ ──────► │ SPEAKING │
  │      │ ◄──────── │          │          │(toolcall)│         │          │
  └──────┘  cancel   └──────────┘          └──────────┘         └─────┬────┘
      ▲                                                               │
      └───────────────────────── TTS done ────────────────────────────┘
```

- `IDLE` — 待机，等待输入或主动搭话决策
- `THINKING` — 推理中（Streaming / 工具决策 / 记忆召回）
- `EXECUTING` — 执行工具调用，完成后回注结果到 THINKING
- `SPEAKING` — TTS 合成 + 前端播放
- `INTERRUPTED` — 中断清理（cancel task → 静音音频 → 回到 IDLE）

### 音频输入管道

```
麦克风 ──► VAD ──► (voice detected) ──► EventBus Priority 0 INTERRUPT
                │
                └─► (speech segment) ──► STT ──► EventBus Priority 2 USER_INPUT
```

VAD 同时承担两个职责：检测到语音起始 → 发送中断；语音段落完整 → 送 STT 识别后作为 USER_INPUT 入队。

## 3. 记忆分层与持久化设计 (Memory Architecture)

为了保证认知清晰，记忆系统进行严格的物理和逻辑隔离：

### MongoDB — Session 上下文（持久化）

存放当前会话窗口（最近 N 轮对话）、当前情感阈值、运行时参数。进程重启后可恢复会话状态。Key-Value 文档模型，按 `session_id` 索引。

### Milvus — 长期记忆（跨会话共享）

严格作为历史记录的向量化存储层。只存放过去发生的对话摘要、日记、知识库文本。不包含运行时参数。当 Agent 在 THINKING 阶段判定需要回忆时，将 Query 向量化在此层做纯语义检索（Top-K），召回的纯文本记录注入 Jinja2 Prompt。

### Working Memory — 推理上下文（内存，不持久化）

每次推理时由 L1 的 `PromptAssembler` 动态组装：从 MongoDB 取最近 N 轮对话 + 从 Milvus 召回 Top-K 相关记忆 + 当前 CharacterConfig + 系统 Prompt 模板，合并为当次 LLM 调用的完整上下文。仅存在于单次推理生命周期中。

```
PromptAssembler
  ├── CharacterConfig (YAML: 人格、情绪阈值、表达风格)
  ├── MongoDB (Session 上下文: 最近 N 轮)
  ├── Milvus  (语义召回: Top-K 相关记忆)
  └── Jinja2 Template (Brain.j2)
        │
        ▼
  完整的 messages[] → LLM
```

## 4. 角色配置 (CharacterConfig)

在 L1 层独立管理，不耦合到 L0 的 PromptManager 中。定义角色的全部静态和动态参数：

```yaml
# character/elysia.yaml
name: Elysia
voice: elysia_v2           # 对应 TTS 音色名
personality:
  traits: [温柔, 好奇, 偶尔毒舌]
  speaking_style: 口语化，短句为主，偶尔加语气词
emotion:
  thresholds:
    excitement: 0.6       # 高于此值触发兴奋表情
    sadness: 0.3
  idle_expressions: [neutral, blink, slight_smile]
wake_words: [Elysia, 小伊]
proactive:
  enabled: true
  interval: 300            # 每 5 分钟检查是否主动搭话
  triggers:
    - type: silence_timeout
      duration: 600         # 10 分钟无交互
    - type: time_of_day
      times: ["08:00", "12:00", "22:00"]
```

在 Prompt 组装和 TTS 音色选择时注入这些参数。

## 5. ReAct 认知循环 (CognitiveLoop)

当用户发起复杂请求（如"查一下我们上周定的计划"），大模型不能直接回答，需要经过 THINKING → EXECUTING → THINKING 的循环。

`CognitiveLoop` 是 L1 层的核心调度器，负责：

```
1. PromptAssembler 组装 messages[]
2. LLM.chat_stream(messages) → THINKING 状态
3. 判断输出：
   ├── 纯文本 → SentenceBuffer → TTS → SPEAKING 状态
   └── tool_calls → EXECUTING 状态
        ├── 执行工具 (asyncio.Task)
        ├── 结果回注 messages[]
        └── 回到步骤 2
```

### 工具注册表 (Tool Registry — L2)

将 Milvus 查询、本地文件读取、系统控制等封装为符合 OpenAI Tool Calling Schema 的标准化 Python 异步函数。注册表是静态字典，L1 的 CognitiveLoop 在组装 LLM 请求时从中提取 tool definitions 注入。

### 防阻塞机制

如果在 EXECUTING 期间（如数据库查询慢）用户发起打断，EventBus Priority 0 直接 `task.cancel()` 当前工具执行和 LLM 推理，丢弃未完成的结果，FSM 回到 IDLE 重新响应用户。

## 6. IdleMonitor — 唤醒与主动搭话

桌面助手的体验不止是"用户说了话然后响应"。`IdleMonitor` 在 IDLE 状态下运行，处理两个场景：

- **唤醒词检测**：用户叫了角色名字才触发完整推理。VAD 检测到语音 → 经轻量唤醒词模型（如 openWakeWord）判断 → 命中则发 `USER_INPUT` 入 EventBus。
- **主动搭话**：根据 CharacterConfig 中的 trigger 规则（静默超时、定时、环境变化），定时评估是否发起对话。决策由轻量级 LLM 调用完成（如"现在适合打招呼吗？"→ yes/no），避免高成本推理。

```
IdleMonitor (IDLE 状态下运行)
  ├── WakeWordDetector: 等待语音 → 唤醒词匹配 → EventBus USER_INPUT
  └── ProactiveTimer: 定时 tick → 评估 trigger → 决策 LLM → 触发对话
```

## 7. 流式数据流转 (Stream Pipeline)

桌面助手的体验很大程度上取决于开口的延迟（TTFT）。从 THINKING 到 SPEAKING 的数据流向：

```
LLM Stream ──► Sentence Buffer ──► TTS Stream ──► OutputManager
   (token)       (分句截断)         (音频 chunk)     ├── WebSocketChannel
                                                     └── ConsoleChannel
```

**分句缓冲策略 (Sentence Buffer)**：大模型逐 token 输出，但 TTS 按字/词合成会导致语气生硬。Sentence Buffer 用正则（匹配 `。，！？.!?`）将文本截断为完整短句后再推给 TTS。多语言扩展通过配置分隔符集合实现（中文 `。，！？`、日文 `。、`、英文 `. ! ? ,`）。

**双队列并行**：TTS 合成（快）和前端播放（慢）之间通过音频队列解耦：合成线程不断向队列写入 audio chunk，前端从队列消费播放。确保长句连续说话时不卡顿。
