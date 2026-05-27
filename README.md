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
| 麦克风采集 + RMS VAD | done |
| STT 语音识别 (SenseVoice) + 情绪标签 | done |
| 用户情绪识别 (STT 声学 HAPPY/SAD/ANGRY...) | done |
| TTS 流式合成 (GPT-SoVits) + 句队列 + 顺序播放 | done |
| 文字/语音同步 (双层缓冲: 服务端 + 浏览器) | done |
| 分句策略 (连续分隔符合并) | done |
| SentenceBuffer 升级 (70 tests: ～…〜/.上下文/可配置) | done |
| 聊天历史 localStorage 持久化 | done |
| 配置驱动入口 (CLI > config.yaml > 默认值) | done |
| /debug 内部状态面板 (记忆/工具/沉淀/连接) | done |
| 优雅退出 (Web /shutdown + 终端 /quit + 浏览器按钮) | done |

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
│   ├── database/         # Milvus / MongoDB / GraphDB
│   ├── input/            # AudioBuffer + WebSocket server
│   └── bench/            # 延迟基准
│
├── core/                 # L1 认知控制 + L2 记忆工具
│   ├── event_bus.py      # 优先级事件总线
│   ├── agent_fsm.py      # 五状态机
│   ├── cognitive_loop.py # ReAct 调度器
│   ├── llm_adapter.py    # LLM 适配器 (真实 + mock)
│   ├── prompt_assembler.py # 提示词组装
│   ├── emotion_engine.py # 情绪引擎
│   ├── sentence_buffer.py # 分句缓冲
│   ├── tool_registry.py  # 工具注册 (OpenAI Schema)
│   ├── consolidator.py   # 记忆沉淀
│   ├── idle_monitor.py   # 空闲监控
│   ├── character_config.py # 角色配置
│   ├── log_setup.py      # 日志系统
│   ├── demo_agent.py     # 组件组装
│   ├── test_phase1.py    # Phase 1 测试 (38)
│   ├── test_phase2.py    # Phase 2 测试 (48)
│   ├── test_phase3.py    # Phase 3 测试 (70)
│   └── memory/
│       ├── session.py    # 会话上下文
│       └── long_term.py  # 长期记忆
│
├── character/
│   ├── elysia.yaml       # 角色定义
│   └── system.j2         # 系统提示词模板 (当前使用)
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
# 全部测试 (core + Module) — 使用 conda env 运行
conda run -n Elysia python -m pytest core/ Module/ -v --asyncio-mode=auto

# 仅 core (不含 Module)
python -m pytest core/ -v --asyncio-mode=auto

# Module 层
python -m pytest Module/ -v
```

**测试统计**: 272 (core) + 243 (Module) = 515 total
