# Elysia Agent

桌面虚拟角色 AI 伴侣。三层架构：L0 基础设施（Module/）→ L1 认知控制（core/）→ L2 记忆工具（core/memory/）。

## 快速开始

```bash
conda activate Elysia

# 设置 API Key
export DEEPSEEK_API_KEY="sk-..."

# 交互模式
python demo.py

# 单次对话
python demo.py --once "你好"

# 详细日志（显示 FSM 转换、情绪变化等）
python demo.py --verbose
```

需要 MongoDB + Milvus 获得持久化记忆，否则自动降级为内存模式。

## 项目结构

```
Agent/
├── demo.py                    # 入口
├── Architecture.md            # 架构设计
├── todo.md                    # 开发计划
│
├── Module/                    # L0 基础设施层
│   ├── llm/                   # DeepSeek / OpenAI / Ollama
│   ├── stt/                   # SenseVoice
│   ├── tts/                   # GPT-SoVits
│   ├── embedding/             # HuggingFace / OpenAI
│   ├── output/                # Console + WebSocket 多通道输出
│   ├── config/                # YAML + env 配置系统
│   ├── logger/                # 日志工厂 (plain / JSON)
│   ├── prompt/                # Jinja2 模板引擎
│   ├── schema/                # 共享数据模型
│   ├── database/              # Milvus / MongoDB / GraphDB
│   └── bench/                 # 延迟基准
│
├── core/                      # L1 认知控制 + L2 记忆工具
│   ├── event_bus.py           # 优先级事件总线 (INTERRUPT > STATE > NORMAL)
│   ├── agent_fsm.py           # 五状态机 (IDLE/THINKING/EXECUTING/SPEAKING/INTERRUPTED)
│   ├── cognitive_loop.py      # ReAct 调度器 (流式 + tool_call 检测)
│   ├── llm_adapter.py         # LLM 适配器 (真实 API + mock 降级)
│   ├── prompt_assembler.py    # 系统提示词组装 (角色 + 记忆 + 会话)
│   ├── emotion_engine.py      # 情绪引擎 (关键词检测 + 衰减)
│   ├── sentence_buffer.py     # 分句缓冲 (供 TTS)
│   ├── tool_registry.py       # OpenAI Tool Schema 工具注册
│   ├── consolidator.py        # 记忆自动沉淀 (LLM 提取事实)
│   ├── idle_monitor.py        # 空闲监控 (唤醒词 + 主动搭话)
│   ├── character_config.py    # YAML 角色配置
│   ├── log_setup.py           # 日志系统配置
│   ├── demo_agent.py          # 组件组装
│   ├── test_phase1.py         # Phase 1 测试 (38)
│   ├── test_phase2.py         # Phase 2 测试 (48)
│   └── memory/
│       ├── session.py         # 会话上下文 (MongoDB + 内存降级)
│       └── long_term.py       # 长期记忆 (Milvus + Embedding + 内存降级)
│
├── character/
│   ├── elysia.yaml            # 角色定义
│   └── system.j2              # 系统提示词模板
│
└── log/                       # 日志输出 (gitignore)
    ├── http.log               # HTTP 请求/响应
    ├── mongo.log              # MongoDB
    ├── milvus.log             # Milvus
    └── embedding.log          # 模型加载
```

## 数据流

```
用户输入 → EventBus(Priority 2) → AgentFSM(THINKING)
  → PromptAssembler(角色 + 会话 + 长期记忆搜索)
  → CognitiveLoop.stream_think()      # 结构化流式调用 LLM
    ├── text delta → ConsoleChannel    # 逐 token 显示
    ├── tool_calls → EXECUTING         # ReAct 循环
    │     → ToolRegistry.execute → 结果回注
    └── 流结束 → SPEAKING → IDLE
      ├── EmotionEngine.analyze()      # 更新心情
      ├── Consolidator.consolidate()   # 提取记忆 (后台)
      └── SessionMemory.save()         # 持久化 (后台)
```

## 测试

```bash
# 全部测试
python -m pytest core/test_phase1.py core/test_phase2.py -v --asyncio-mode=auto

# 仅单元测试 (无网络)
python -m pytest core/test_phase1.py core/test_phase2.py -v --asyncio-mode=auto -k "not Integration"

# Module 层测试
python -m pytest Module/ -v
```

## 环境

- Python 3.12+
- Conda 环境: `conda activate Elysia`
- 可选: MongoDB (session 持久化), Milvus (向量搜索)
- API: DeepSeek API Key (`DEEPSEEK_API_KEY` 环境变量)
