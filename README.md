# Elysia Agent

桌面虚拟角色 AI 伴侣。三层架构：基础设施 → 认知控制 → 记忆工具。

## 快速开始

```bash
conda activate Elysia

# 设置 API Key（可选，不设则用内置 mock）
export DEEPSEEK_API_KEY="sk-..."

# 交互模式
python demo.py

# 单次对话
python demo.py --once "你好"

# 详细日志
python demo.py --once "你好" --verbose
```

## 项目结构

```
Agent/
├── demo.py                    # 入口：交互 REPL / 单次对话
├── Architecture.md            # 架构设计方案
├── todo.md                    # 下一步计划
│
├── Module/                    # L0 基础设施层（共享库）
│   ├── llm/                   # 大模型（DeepSeek/OpenAI/Ollama）
│   ├── stt/                   # 语音识别（SenseVoice）
│   ├── tts/                   # 语音合成（GPT-SoVits）
│   ├── embedding/             # 文本向量化（HuggingFace/OpenAI）
│   ├── output/                # 多通道输出（Console/WebSocket）
│   ├── config/                # 配置基础设施（YAML + env + 校验）
│   ├── logger/                # 日志工厂
│   ├── prompt/                # Jinja2 模板引擎
│   ├── schema/                # 共享数据模型
│   ├── database/              # 向量/文档/图数据库后端
│   └── bench/                 # 延迟基准测试
│
├── core/                      # L1 认知控制层 + L2 记忆工具层
│   ├── event_bus.py           # 优先级事件总线
│   ├── agent_fsm.py           # 五状态有限状态机
│   ├── cognitive_loop.py      # ReAct 认知循环调度器
│   ├── llm_adapter.py         # LLM 适配器（工具调用 + mock 降级）
│   ├── prompt_assembler.py    # 提示词组裝
│   ├── tool_registry.py       # OpenAI Tool Schema 工具注册表
│   ├── sentence_buffer.py     # 分句缓冲（供 TTS 用）
│   ├── idle_monitor.py        # 空闲监控（唤醒词 + 主动搭话）
│   ├── character_config.py    # YAML 角色配置加载
│   ├── demo_agent.py          # 组件组装 + 种子数据 + 演示工具
│   └── memory/
│       ├── session.py         # 会话上下文
│       └── long_term.py       # 长期记忆检索
│
└── character/
    ├── elysia.yaml            # 角色定义（人格/唤醒词/情绪）
    └── system.j2              # 系统提示词模板
```

## 架构

三层 Protocol-Registry-Backend 模式：

```
L0  Module/        无状态原子能力  ── LLM, STT, TTS, Embedding, Database, Output
         │
L1  core/          认知中枢       ── EventBus → AgentFSM → CognitiveLoop → PromptAssembler
         │
L2  core/memory/   记忆与工具     ── SessionMemory, LongTermMemory, ToolRegistry
```

完整数据流：

```
用户输入 → EventBus → AgentFSM(THINKING) → PromptAssembler(角色+记忆+模板)
  → CognitiveLoop → LLM.think() →
    ├── tool_calls → EXECUTING → ToolRegistry.execute → 回注 → THINKING
    └── text → SPEAKING → OutputManager → [ConsoleChannel, WebSocketChannel, ...]
      → IDLE
```

## 测试

```bash
# L0 单元测试
python -m pytest Module/ -v

# 集成冒烟测试（需要对应服务）
python -m Module.llm          # DEEPSEEK_API_KEY
python -m Module.stt          # SenseVoice localhost:20042
python -m Module.tts          # GPT-SoVits localhost:9880
python -m Module.embedding    # OPENAI_API_KEY
python -m Module.output       # 自包含
python -m Module.bench        # 延迟基准
```

## 添加新后端

```python
# Module/<service>/your_backend.py
from .protocol import SomeBackend

class YourBackend(SomeBackend):
    def __init__(self, model, *, api_key, base_url, logger=None):
        ...
    async def the_method(self, ...):
        ...

# Module/<service>/registry.py _BACKENDS
_BACKENDS["your-provider"] = {
    "base_url": "...", "env_key": "YOUR_API_KEY", ...
}
```
