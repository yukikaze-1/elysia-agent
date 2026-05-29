# Elysia Agent

桌面 AI 伴侣，支持语音对话。六阶段全部完成。

## 快速开始

```bash
conda activate Elysia
export DEEPSEEK_API_KEY="sk-..."

# 嵌入模型缓存（二选一）
export MODEL_CACHE_DIR="/path/to/model_cache"   # 方式1：环境变量
# 或在项目根创建 .model_cache/ 软链接          # 方式2：项目本地

# 交互模式 (stdin)
python demo.py

# 单次对话
python demo.py --once "你好"

# WebSocket 服务器模式（浏览器访问 http://localhost:8765）
python demo.py --server
#   退出: 终端输入 /quit、浏览器设置面板"关闭服务"、或 curl -X POST /shutdown

# 启用 TTS + STT（或在 config.yaml 中设置 TTS.enabled / STT.enabled）
python demo.py --server --tts --stt

# Debug 面板（浏览器访问 http://localhost:8765/debug）

# 详细日志
python demo.py --verbose
```

## 架构

三层 Protocol-Registry-Backend：

```
L0  Module/        无状态原子能力 — LLM, STT, TTS, Embedding, Database, Output
L1  core/          认知中枢 — EventBus → AgentFSM → CognitiveLoop → PromptAssembler
L2  core/memory/   记忆与工具 — SessionMemory, LongTermMemory, ToolRegistry
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
| 测试补充 (507 → 547，+40 tests + 修 2 bug) | done |
| 日志链路完整追踪 (request ID + 毫秒时间戳 + 全管线计时) | done |
| 对话日志分离 (log/conversation.log) | done |
| TTS 句编号 (本轮/全局双计数) | done |

## 项目结构

```
Agent/
├── demo.py              # 入口
├── config.yaml           # 总配置
├── Architecture.md       # 架构设计
├── todo.md               # 开发计划 + 已知问题
├── test_plan.md          # 测试计划
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
│   ├── tool/             # 跨 Provider 工具系统 (172 tests)
│   ├── database/         # Milvus / MongoDB / GraphDB
│   ├── input/            # AudioBuffer + WebSocket server
│   └── bench/            # 延迟基准
│
├── core/                 # L1 认知控制
│   ├── schema.py         #   共享类型 (AgentState/EventType/EventPriority)
│   ├── event_bus.py      #   优先级事件总线
│   ├── agent_fsm.py      #   五状态机
│   ├── cognitive_loop.py #   ReAct 调度器
│   ├── llm_adapter.py    #   LLM 适配器
│   ├── prompt_assembler.py # 提示词组装 + 记忆检索
│   ├── emotion_engine.py #   情绪引擎
│   ├── sentence_buffer.py # 分句缓冲
│   ├── consolidator.py   #   记忆沉淀 (缓冲6条+rerank)
│   ├── idle_monitor.py   #   空闲监控
│   ├── character_config.py # 角色配置
│   ├── log_setup.py      #   日志系统
│   ├── app_config.py     #   ConfigLoader 类型化配置
│   ├── debug_info.py     #   /debug 面板数据聚合
│   ├── demo_agent.py     #   组件组装 + 生命周期
│   ├── tools/            #   Agent 工具集
│   ├── memory/           #   记忆子系统
│   │   ├── session.py    #     会话上下文
│   │   └── long_term.py  #     长期记忆
│   └── tests/            #   测试
│       ├── fake_llm.py           # 测试用 LLM backend
│       ├── test_phase1.py        # Phase 1 (认知核心)
│       ├── test_phase2.py        # Phase 2 (记忆 + 工具)
│       ├── test_phase3.py        # Phase 3 (生命周期)
│       └── test_sentence_buffer.py # SentenceBuffer (70)
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
│   └── index.html        # 浏览器客户端
│
└── log/                  # 日志 (gitignore)
```

## 测试

```bash
# 全部测试
python -m pytest core/tests/ Module/ -v --asyncio-mode=auto

# 仅 core
python -m pytest core/tests/ -v --asyncio-mode=auto

# Module 层
python -m pytest Module/ -v

# 特定文件
python -m pytest core/tests/test_sentence_buffer.py -v
python -m pytest Module/input/test_server.py -v --asyncio-mode=auto
```

**测试统计**: 251 (core) + 463 (Module) = 714 total (+3 pre-existing failures)
