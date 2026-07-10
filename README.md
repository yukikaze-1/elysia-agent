# Elysia Agent

桌面 AI 伴侣，支持语音对话、内心独白、工具调用和后台任务。

> 最近 (2026-07-03): Brain Prompt 提供者架构——11 个 PromptSection 独立模块，自管数据、自管模板。2026-06-26: Module/_common/ 轻量共享层重构——消除 6 个 registry + 5 个 test() + 9 个 __main__ + 6 个 test_config 重复代码，971 测试零回归。

---

## 快速开始

### 1. 一键安装

```bash
bash scripts/setup.sh
```

脚本会自动: 检测/创建 conda 环境 → pip install → 配置 .env → 环境预检。

`config.yaml` 中可调整 model、TTS provider 等。大部分有默认值，开箱可用。

### 2. 数据库（必需）

前置条件：安装 [Docker](https://docs.docker.com/engine/install/) + Docker Compose 插件。

```bash
# 启动 MongoDB + Milvus
docker compose up -d

# 查看运行状态
docker compose ps

# 停止
docker compose down
```

端口与 `.env` 默认值一致（MongoDB 27017，Milvus 19530），无需额外配置。

**数据持久化：** 数据存储在 Docker volume 中，重启不丢失。如需彻底清除：

```bash
docker compose down -v   # 删除所有数据！
```

### 3. 启动

```bash
# WebSocket 服务器模式（浏览器访问 http://localhost:8765）
python app.py --server

# 启用 TTS + STT（需相应服务已部署或 API key 已配置）
python app.py --server --tts --stt

# Debug 面板（http://localhost:8765/debug）
# 交互模式 (stdin)
python app.py
```

### 服务依赖一览

| 功能 | 必需？ | 服务/API | 环境变量 |
|------|--------|----------|----------|
| 对话 | **是** | DeepSeek API | `DEEPSEEK_API_KEY` |
| 数据库 | **是** | MongoDB + Milvus (Docker) | `MONGODB_URI` / `MILVUS_URI` |
| 语音合成 | 否 | MiniMax / GPT-SoVits / OpenAI TTS | `MINIMAX_API_KEY` / `GPT_SOVITS_URL` / `CUSTOM_TTS_API_KEY` |
| 语音识别 | 否 | SenseVoice（本地） / OpenAI STT | `STT_API_URL` / `OPENAI_API_KEY` |
| 网页搜索 | 否 | Tavily API | `TAVILY_API_KEY` |
| 图片识别 | 否 | MiMo API | `MIMO_API_KEY` |
| 嵌入模型 | 否 | HuggingFace（本地）/ OpenAI | `OPENAI_API_KEY` |

不配置的可选服务会自动降级或跳过，不影响文字对话功能。
数据库是必需的——`app.py --check --full` 会验证连通性。

---

## 架构

```
L0  Module/        无状态原子能力 — LLM, STT, TTS, Embedding, Database, Output, _common/, prompt/
L1  core/          认知中枢 — EventBus → AgentFSM → CognitiveLoop → PromptAssembler
L2  core/memory/   记忆与工具 — SessionMemory, LongTermMemory, ToolManager (Plugin 架构)
L2  Module/task_system/  后台任务 — Planner → Executor → Evaluator (DAG 编排)
```

数据流：

```
用户输入 → EventBus → FSM(THINKING) → PromptAssembler(角色+记忆+感官)
  ├── STT 情绪标签 → EmotionEngine + SystemPromptManager <perception>
  → CognitiveLoop.stream_think()
    ├── <inner_voice> 标签检测 (PRE→THINK→REPLY 状态机)
    │     ├── inner_voice → 前端淡色思考气泡
    │     └── public_reply → SentenceBuffer → TTS → 语音播放
    ├── text delta → Console/WS
    └── tool_calls → EXECUTING → 回注
  → EmotionEngine(心情) → Consolidator(记忆沉淀) → IDLE
```

## 伴侣化改造

| 步骤 | 状态 | 内容 |
|------|------|------|
| 🥇 对话风格 | ✅ | Brain Prompt 模块化——11 个独立 section，自然对话/克制/关系模型 |
| 🥈 内心世界 | ✅ | `<inner_voice>` 标签输出 + 前端气泡 + 持久化 + 日志 |
| 🥉 记忆人格化 | 待做 | 记忆融入对话，不是"引用"是"记得" |
| 🏅 主动性 | 待做 | 注意到离开/回来，深夜关心 |

详见 [`docs/companion-roadmap.md`](docs/companion-roadmap.md) 和 [`docs/companion-prompt-guide.md`](docs/companion-prompt-guide.md)

---

## 功能

| 功能 | 状态 |
|------|------|
| 流式对话 + 中断恢复 | done |
| **inner_voice 内心独白** (think 气泡 + 持久化 + debug 查看器) | done |
| 情绪检测 + 表情推送 | done |
| MongoDB/Milvus 持久化 (健康检查自动重连) | done |
| 记忆自动沉淀 (LLM 提取事实) | done |
| HuggingFace embedding 离线加载 | done |
| WebSocket 服务器 + HTML 客户端 | done |
| 麦克风采集 + WebRTC VAD | done |
| STT 语音识别 (SenseVoice) + 情绪标签 | done |
| TTS 流式合成 (GPT-SoVits) + 句队列 | done |
| 文字/语音绑定 + 逐句独立气泡 | done |
| **TaskSystem v1** — DAG 编排 + Planner/Executor/Evaluator (334 tests) | done |
| **Token 追踪** — TokenTracker + PromptRecorder + 前端标签 | done |
| 工具系统 Plugin 架构 — 12 插件 + SafetyPipeline + HITL 审批 | done |
| **模型切换** — DeepSeek/OpenAI/Ollama/MiMo + 运行时热切换 | done |
| **多模态** — 图片上传/预览/lightbox + describe_image 视觉工具 | done |
| Live2D 虚拟角色渲染 (Cubism SDK) | done |
| 前端 JS 模块化 — 12 个 ES 模块 | done |
| 配置系统 (YAML + env + 类型校验) | done |
| /debug 面板 (FSM/记忆/工具/Token/DAG/LLM Prompt/Raw Response) | done |
| 优雅退出 + /health /ready 端点 | done |
| LLM 重试 (指数退避) + 请求超时 | done |
| 对话日志分离 (log/conversation.log) | done |
| 会话恢复 (localStorage + 服务端持久化) | done |

---

## 项目结构

```
Agent/
├── app.py                  # 入口
├── config.yaml             # 总配置
├── requirements.txt        # Python 依赖
├── docker-compose.yml       # 数据库服务
├── scripts/                 # 安装工具
│   ├── setup.sh             #   一键安装
│   └── download_model.py    #   嵌入模型下载
├── todo.md                 # 开发计划 + 已知 Bug
│
├── Module/                 # L0 基础设施层
│   ├── _common/            #   共享工具 (registry_helpers, test_helpers, main_runner, ...)
│   ├── llm/                # DeepSeek / OpenAI / Ollama / MiMo
│   ├── stt/                # SenseVoice / OpenAI-compatible
│   ├── tts/                # GPT-SoVits / OpenAI-compatible
│   ├── embedding/          # HuggingFace / OpenAI
│   ├── output/             # Console + WebSocket 多通道
│   ├── config/             # YAML + env 配置系统
│   ├── logger/             # 日志工厂
│   ├── prompt/             # Brain Prompt 引擎 — SystemPromptManager + 11 PromptSections
│   ├── schema/             # 共享数据模型
│   ├── tool/               # 工具框架 (172 tests)
│   ├── database/           # Milvus / MongoDB / GraphDB
│   ├── input/              # AudioBuffer + WebSocket server
│   ├── task_system/        # TaskSystem v1 (334 tests)
│   └── bench/              # 延迟基准
│
├── core/                   # L1 认知控制
│   ├── agent.py            #   组件组装 + 生命周期
│   ├── cognitive_loop.py   #   ReAct 调度器 + inner_voice 状态机
│   ├── llm_adapter.py      #   LLM 适配器 (含重试)
│   ├── prompt_assembler.py #   提示词组装 + 记忆检索（调用 SystemPromptManager）
│   ├── agent_fsm.py        #   状态机 (IDLE→THINKING→EXECUTING→SPEAKING→IDLE_BACKGROUND_TASK)
│   ├── event_bus.py        #   优先级事件总线
│   ├── schema.py           #   共享类型
│   ├── emotion_engine.py   #   情绪引擎
│   ├── sentence_buffer.py  #   分句缓冲
│   ├── block_filter.py     #   结构化块过滤 (code/markdown)
│   ├── text_utils.py       #   文本提取 + MessageObj
│   ├── message_id.py       #   消息 ID 生成
│   ├── consolidator.py     #   记忆沉淀
│   ├── idle_monitor.py     #   空闲监控
│   ├── character_config.py #   角色配置
│   ├── app_config.py       #   ConfigLoader 配置
│   ├── log_setup.py        #   日志系统 + conversation.log
│   ├── debug_info.py       #   /debug 面板数据聚合
│   ├── task_scheduler.py   #   Worker Pool (5 workers)
│   ├── token_tracker.py    #   Token 消耗追踪
│   ├── prompt_recorder.py  #   LLM Prompt 记录器 (环形缓冲)
│   ├── response_recorder.py #  LLM Response 记录器 (环形缓冲)
│   ├── timing.py           #   性能计时
│   ├── tag_parser.py       #   标签解析
│   ├── tools/              #   工具插件 (12 个)
│   ├── memory/             #   记忆子系统 (session + long_term)
│   └── tests/              #   测试 (~367 tests)
│
├── prompt/                 # 反射模板 (Amygdala/ActiveSpeak/MicroReflector/MacroReflector 参考, CharacterReflector 活跃)
│   └── CharacterReflector.j2 #   角色进化反射
│
├── static/                 # 浏览器客户端
│   ├── index.html          #   入口
│   ├── debug.html          #   Debug 面板
│   ├── css/chat.css        #   样式 (含 .inner-voice-bubble)
│   └── js/                 #   ES 模块 (config/state/ws/chat/audio/mic/settings/persistence/main)
│
├── docs/                   # 文档
│   ├── architecture.md     #   架构详解
│   ├── frontend-api.md     #   前端 API 文档
│   ├── completed.md        #   已完成记录
│   ├── companion-roadmap.md #  伴侣化改造路线图
│   ├── companion-prompt-guide.md # Prompt 编写指南
│   └── superpowers/        #   设计规范 + 实施计划 (已归档)
│
└── log/                    # 日志 (gitignore)
```

---

## 测试

```bash
conda activate Elysia

# TaskSystem 单元测试 (~305 tests)
python -m pytest Module/task_system/tests/unit/ -v

# TaskSystem 集成测试 (~36 tests)
python -m pytest Module/task_system/tests/integration/ -v

# Core 测试 (~361 tests)
python -m pytest core/tests/ -v

# Module 测试 (~610 tests, 含 _common/ 22 tests)
python -m pytest Module/ -v --ignore=Module/task_system

# 全部测试 (~1,370 tests)
python -m pytest Module/ core/ -v
```

**测试统计**: ~1,370 tests（305 TaskSystem unit + 36 TaskSystem integration + 361 core + 610 Module + 82 core/tools）
