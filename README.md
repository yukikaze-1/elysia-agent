# Elysia Agent

桌面 AI 伴侣，支持语音对话。核心功能完整，持续迭代优化中。

> 最近: 2026-06-05 debug 页面图片显示 + describe_image 视觉工具 + 多模态 Phase 2 前端图片交互 + 项目清理，详见 `todo.md` 已完成

## 快速开始

```bash
conda activate Elysia
export DEEPSEEK_API_KEY="sk-..."

# 嵌入模型缓存（二选一）
export MODEL_CACHE_DIR="/path/to/model_cache"   # 方式1：环境变量
# 或在项目根创建 .model_cache/ 软链接          # 方式2：项目本地

# 交互模式 (stdin)
python app.py

# 单次对话
python app.py --once "你好"

# WebSocket 服务器模式（浏览器访问 http://localhost:8765）
python app.py --server
#   退出: Ctrl+C、浏览器设置面板"关闭服务"、或 curl -X POST /shutdown

# 启用 TTS + STT（或在 config.yaml 中设置 TTS.enabled / STT.enabled）
python app.py --server --tts --stt

# Debug 面板（浏览器访问 http://localhost:8765/debug）

# 详细日志
python app.py --verbose
```

## 架构

三层 Protocol-Registry-Backend：

```
L0  Module/        无状态原子能力 — LLM, STT, TTS, Embedding, Database, Output
L1  core/          认知中枢 — EventBus → AgentFSM → CognitiveLoop → PromptAssembler
L2  core/memory/   记忆与工具 — SessionMemory, LongTermMemory, ToolManager (Plugin 架构)
```

数据流：

```
用户输入 → EventBus → FSM(THINKING) → PromptAssembler(角色+记忆)
  ├── STT 情绪标签 (SenseVoice 自带) → EmotionEngine + Brain <perception>
  → CognitiveLoop.stream_think()     # 流式 LLM + 工具调用检测
    ├── text delta → Console/WS        # 逐 token 显示
    ├── SentenceBuffer → TTS 流式 → 浏览器播放
    └── tool_calls → EXECUTING → 回注
  → EmotionEngine(心情) → Consolidator(记忆沉淀, 后台) → IDLE
```

## 功能

| 功能 | 状态 |
|------|------|
| 流式对话 (Token 级延迟) | done |
| 中断 + 恢复 | done |
| 情绪检测 + 表情推送 | done |
| MongoDB/Milvus 持久化 (自动降级) | done |
| 记忆自动沉淀 (LLM 提取事实) | done |
| HuggingFace embedding 离线加载 | done |
| WebSocket 服务器 + HTML 客户端 | done |
| 麦克风采集 + WebRTC VAD | done |
| STT 语音识别 (SenseVoice) + 情绪标签 | done |
| 用户情绪识别 (STT 声学 HAPPY/SAD/ANGRY...) | done |
| TTS 流式合成 (GPT-SoVits) + 句队列 + 顺序播放 | done |
| 文字/语音绑定发送 (TTS task 等音频后绑在一起发 delta+audio) | done |
| 逐句独立消息气泡 + 音频播放完才弹出下一条 | done |
| 分句策略 (连续分隔符合并) | done |
| 并行工具调用 (asyncio.gather) | done |
| 记忆结构化 Schema + rerank + 缓冲提取 | done |
| VAD 打断保护 (THINKING/EXECUTING 不打断) | done |
| Emoji 按 TTS 句排队发送 | done |
| SentenceBuffer 升级 (70 tests: ～…〜/.上下文/可配置) | done |
| 聊天历史 localStorage 持久化 | done |
| 配置系统 (ConfigLoader: YAML + env + 类型校验) | done |
| /debug 内部状态面板 (记忆/工具/沉淀/连接) | done |
| 优雅退出 (Web /shutdown + 终端 /quit + 浏览器按钮) | done |
| WebRTC VAD (GMM 声学模型) + nospeech 过滤 | done |
| 事件循环修复 (asyncio.Lock + 轮询等 IDLE + EventBus FIFO) | done |
| 日志系统修复 (RotatingFile + 级别修正 + 查漏补缺) | done |
| 代码清理 (MockLLMBackend/system.j2 移除 + ConfigLoader 接入) | done |
| 测试补充 (766 → 883，+117 tests) | done |
| 日志链路完整追踪 (request ID + 毫秒时间戳 + 全管线计时) | done |
| 对话日志分离 (log/conversation.log) | done |
| TTS 句编号 (本轮/全局双计数) | done |
| Live2D 虚拟角色渲染 (Cubism SDK for Web) | done |
| 前端布局重构 (响应式三栏, Live2D 满高) | done |
| 工具调用内联卡片 (展开/折叠, spinner→✓/✗) | done |
| 工具系统 Plugin 架构 (SafetyPipeline 6 阶段中间件) | done |
| EmbeddingFactory 全局单例缓存 (多实例共享模型) | done |
| 统一命名移除 demo 前缀 (app.py, core/agent.py) | done |
| 文档整理 (已完成记录分离到 docs/completed.md) | done |
| PromptAssembler 复用 PromptManager | done |
| SentenceBuffer 死代码清理 (_skip_dot_and_retry) | done |
| ToolContext.user_id 配置化 | done |
| filesystem .env 精确匹配 | done |
| os._exit(0) + stdin watcher 移除 (优雅退出) | done |
| TaskScheduler Worker Pool (5 workers, retry, cancel) | done |
| FIFO 消息排队 (busy 时暂存 + 容量上限 32) | done |
| LLM 请求自动重试 (429/5xx, 指数退避) | done |
| MemoryConsolidator 并发保护 (asyncio.Lock) | done |
| LongTermMemory TTL 驱逐 (90 天 + 1 万条上限) | done |
| LongTermMemory Milvus 脏数据回写同步 | done |
| CognitiveLoop/TaskScheduler 公开 API 封装 | done |
| EmotionEngine 异步任务异常边界 | done |
| SessionMemory MongoDB 原子 upsert | done |
| TaskScheduler prune 并发安全 (独立周期任务) | done |
| 文件系统安全强化 (扩展 denylist) | done |
| 消息气泡悬停复制按钮 | done |
| 发送按钮状态动画 (✓ 反馈) | done |
| /health /ready 健康检查端点 | done |
| `.env.example` 模板 + API key 轮换指引 | done |
| `requirements.txt` 依赖声明 | done |

## 项目结构

```
Agent/
├── requirements.txt       # Python 依赖
├── .env.example           # 环境变量模板
├── app.py              # 入口
├── config.yaml           # 总配置
├── docs/architecture.md  # 架构设计
├── todo.md               # 开发计划 + 已知问题
│
├── Module/               # L0 基础设施层
│   ├── llm/              # DeepSeek / OpenAI / Ollama
│   ├── stt/              # SenseVoice
│   ├── tts/              # GPT-SoVits
│   ├── embedding/        # HuggingFace / OpenAI
│   ├── output/           # Console + WebSocket 多通道
│   ├── config/           # YAML + env 配置系统
│   ├── logger/           # 日志工厂
│   ├── prompt/           # Jinja2 模板
│   ├── schema/           # 共享数据模型
│   ├── tool/             # 工具框架 — ToolPlugin/SafetyPipeline/ToolManager (172 tests)
│   ├── database/         # Milvus / MongoDB / GraphDB
│   ├── input/            # AudioBuffer + WebSocket server
│   └── bench/            # 延迟基准
│
├── core/                 # L1 认知控制
│   ├── schema.py         #   共享类型 (AgentState/EventType/EventPriority)
│   ├── event_bus.py      #   优先级事件总线
│   ├── agent_fsm.py      #   五状态机
│   ├── cognitive_loop.py #   ReAct 调度器
│   ├── llm_adapter.py    #   LLM 适配器 (含重试)
│   ├── prompt_assembler.py # 提示词组装 + 记忆检索
│   ├── emotion_engine.py #   情绪引擎 (含异常边界)
│   ├── sentence_buffer.py # 分句缓冲
│   ├── consolidator.py   #   记忆沉淀 (缓冲6条 + 并发锁)
│   ├── idle_monitor.py   #   空闲监控
│   ├── character_config.py # 角色配置
│   ├── log_setup.py      #   日志系统
│   ├── app_config.py     #   ConfigLoader 类型化配置
│   ├── debug_info.py     #   /debug 面板数据聚合
│   ├── task.py           #   Task 数据模型
│   ├── task_scheduler.py #   Worker Pool (5 workers + prune loop)
│   ├── timing.py         #   性能计时
│   ├── agent.py          #   组件组装 + 生命周期
│   ├── tools/            #   Agent 工具集
│   ├── memory/           #   记忆子系统
│   │   ├── session.py    #     会话上下文 (原子 upsert)
│   │   └── long_term.py  #     长期记忆 (TTL + 脏回写)
│   └── tests/            #   测试
│       ├── fake_llm.py           # 测试用 LLM backend
│       ├── test_phase1.py        # Phase 1 (认知核心)
│       ├── test_phase2.py        # Phase 2 (记忆 + 工具)
│       ├── test_phase3.py        # Phase 3 (生命周期)
│       ├── test_sentence_buffer.py # SentenceBuffer (70)
│       ├── test_task_model.py    # Task model (15)
│       ├── test_task_scheduler.py # Scheduler (26)
│       └── test_log_setup.py     # Log setup (3)
│
├── character/
│   └── elysia.yaml       # 角色定义
│
├── prompt/               # 多层认知模板
│   ├── Brain.j2          #   L1 主脑 — 双轨思维协议 (已接入)
│   ├── Amygdala.j2       #   L0 潜意识 — 情绪感知 (待接入)
│   ├── ActiveSpeak.j2    #   L1 主动说话决策
│   ├── MicroReflector.j2 #   L2 即时反思 — 记忆提取
│   ├── MacroReflector.j2 #   L2 日终反思 — 日记/情感趋势
│   └── Utils.j2          #   工具宏
│
├── static/
│   └── index.html        # 浏览器客户端 (含复制按钮、发送动画)
│
├── docs/                  # 文档
│   ├── architecture.md               # 架构详解
│   ├── completed.md                  # 已完成开发记录（归档）
│   ├── review-2026-06-01.md          # 全面代码审查报告
│   ├── sync-lessons.md               # 文字/音频同步经验教训
│   ├── ref_memory-architecture.md    # 记忆架构参考
│   └── superpowers/
│       ├── specs/                    # 设计规范（5 篇）
│       └── plans/archive/            # 实现计划（22 篇，全部已完成）
│
└── log/                  # 日志 (gitignore)
```

## 测试

```bash
conda activate Elysia

# 全部测试
python -m pytest Module/ core/ -v

# 仅 core
python -m pytest core/tests/ -v

# Module 层
python -m pytest Module/ -v

# 特定文件
python -m pytest core/tests/test_sentence_buffer.py -v
python -m pytest core/tests/test_task_scheduler.py -v
```

**测试统计**: 883 total（5 pre-existing failures）
