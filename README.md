# Elysia Agent

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Linux](https://img.shields.io/badge/platform-Linux-orange.svg)](#)

桌面 AI 伴侣，支持语音对话、内心独白、工具调用和后台任务。

> **运行环境：Linux**（Windows/Mac 用户请使用 WSL2 或虚拟机）

> 最近 (2026-07-03): Brain Prompt 提供者架构——11 个 PromptSection 独立模块，自管数据、自管模板。2026-06-26: Module/_common/ 轻量共享层重构——消除 6 个 registry + 5 个 test() + 9 个 __main__ + 6 个 test_config 重复代码，971 测试零回归。

---

## 快速开始

### 1. 下载项目

```bash
git clone <仓库地址>   # 点击页面上绿色「Code」按钮复制
cd Agent
```

### 2. 装好三样东西

- **[Miniconda](https://docs.anaconda.com/miniconda/install/)** — Python 环境管理。装好后在终端输入 `conda --version` 确认
- **[Docker Engine](https://docs.docker.com/engine/install/ubuntu/)** — 安装后自带 Docker Compose 插件。装好后输入 `docker --version` 确认
- **[DeepSeek API key](https://platform.deepseek.com/api_keys)** — 免费注册，在左侧「API Keys」页面点"创建 API key"，复制备用

> 系统自带 Python >= 3.11 可替代 conda

### 3. 首次安装（只需一次）

```bash
# 安装环境 + 下载模型（约 1.3GB，需网络）
bash scripts/setup.sh

# 编辑 .env，填入 API key（nano .env，或用桌面文本编辑器打开）
#   必填: DEEPSEEK_API_KEY
#   语音: MINIMAX_API_KEY（TTS，免费注册 https://platform.minimaxi.com）
# 更多可选配置见 .env.example 中的注释

# 启动数据库（Docker 容器，重启后自动保持运行）
docker compose up -d
```

### 启动（每次使用）

```bash
bash scripts/start.sh
```

自动完成：conda 激活 → 数据库检查 → 快速预检 → 启动服务（WebSocket 服务器）。

打开浏览器访问 **`http://localhost:8765`**，在聊天界面与 AI 对话。日志输出到 `log/app.log`。停止：`Ctrl+C`。

### 文件位置

| 文件/目录 | 用途 |
|-----------|------|
| `log/app.log` | 应用运行日志（10MB×3 轮转） |
| `log/conversation.log` | 所有对话记录 |
| `log/mongo.log`、`milvus.log`、`http.log` 等 | 底层库日志（通常不需要看） |
| `.model_cache/` | 嵌入模型存放位置（`BAAI/bge-large-zh-v1.5/`，约 1.3GB） |

> `scripts/download_model.py` 将模型下载到 `.model_cache/`。运行时按 `MODEL_CACHE_DIR` → `.model_cache/` → `~/.cache/huggingface/hub/` 顺序查找。

### 高级用法（可选）

```bash
python app.py --cli              # 交互终端模式（stdin 打字）
python app.py --check            # 环境预检
python app.py --check --full     # 含连通性探活
python app.py --host 0.0.0.0 --port 8080  # 自定义端口
python app.py --verbose          # 详细日志（调试用）
python app.py --config custom.yaml --character custom/char.yaml  # 自定义配置
```

### 故障排查

**端口被占用**
```bash
# 查看谁占用了 8765 端口
lsof -i :8765
# 换端口启动
python app.py --port 8766
```

**Docker 起不来**
```bash
docker compose ps          # 确认服务状态
docker compose logs milvus # 查看日志
docker compose restart     # 重启
```

**预检报错**
```bash
python app.py --check --full  # 完整预检，准确定位问题
python scripts/download_model.py  # 缺少嵌入模型
```

**conda 环境不存在**
```bash
# 重新运行安装脚本
bash scripts/setup.sh
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
| 嵌入模型 | **是** | BAAI/bge-large-zh-v1.5 (HuggingFace，默认 hf-mirror.com 镜像) | `MODEL_CACHE_DIR` |

不配置的可选服务会自动降级或跳过，不影响文字对话功能。
数据库和嵌入模型是必需的——`app.py --check` 会验证。

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
| MongoDB/Milvus 持久化 + 健康检查自动重连 | done |
| 记忆自动沉淀 (LLM 提取事实) | done |
| HuggingFace embedding 离线加载 (BAAI/bge-large-zh-v1.5) | done |
| WebSocket 服务器 + HTML 客户端 | done |
| 麦克风采集 + WebRTC VAD | done |
| STT 语音识别 (SenseVoice / OpenAI-compatible) + 情绪标签 | done |
| TTS 流式合成 (MiniMax / GPT-SoVits / OpenAI-compatible) + 句队列 | done |
| 文字/语音绑定 + 逐句独立气泡 | done |
| **TaskSystem v1** — DAG 编排 + Planner/Executor/Evaluator (334 tests) | done |
| **Token 追踪** — TokenTracker + PromptRecorder + 前端标签 | done |
| 工具系统 Plugin 架构 — 12 插件 + SafetyPipeline + HITL 审批 | done |
| **模型切换** — DeepSeek/OpenAI/Ollama/MiMo + 运行时热切换 | done |
| **多模态** — 图片上传/预览/lightbox + describe_image 视觉工具 | done |
| Live2D 虚拟角色渲染 (Cubism SDK) | done |
| 前端 JS 模块化 — 12 个 ES 模块 | done |
| 配置系统 (YAML + env + 类型校验 + providers.yaml) | done |
| /debug 面板 (FSM/记忆/工具/Token/DAG/LLM Prompt/Raw Response) | done |
| 优雅退出 + /health /ready 端点 | done |
| LLM 重试 (指数退避) + 请求超时 | done |
| 日志系统 — stderr + log/app.log + log/conversation.log + 库日志分离 | done |
| 会话恢复 (localStorage + 服务端持久化) | done |
| **一键部署** — setup.sh + start.sh + download_model.py | done |
| **环境预检** — app.py --check / --check --full (14 项) | done |
| **Docker 数据库** — docker-compose.yml (MongoDB 7 + Milvus v2.4) | done |

---

## 项目结构

```
Agent/
├── app.py                  # 入口
├── config.yaml             # 总配置
├── requirements.txt        # Python 依赖
├── docker-compose.yml      # 数据库服务
├── Makefile                # 测试/检查命令
├── pytest.ini              # pytest 配置（integration 标记）
├── .env.example            # 环境变量模板
├── scripts/                # 安装/启动工具
│   ├── setup.sh            #   一键安装
│   ├── start.sh            #   一键启动
│   └── download_model.py   #   嵌入模型下载
├── todo.md                 # 开发计划 + 已知 Bug
├── log/                    # 日志输出 (gitignore)
├── data/                   # 数据存储 (gitignore)
│
├── Module/                 # L0 基础设施层
│   ├── _common/            #   共享工具 (registry_helpers, test_helpers, main_runner, ...)
│   ├── llm/                # DeepSeek / OpenAI / Ollama / MiMo
│   ├── stt/                # SenseVoice / OpenAI-compatible
│   ├── tts/                # GPT-SoVits / MiniMax / OpenAI-compatible
│   ├── embedding/          # HuggingFace / OpenAI
│   ├── output/             # Console + WebSocket 多通道
│   ├── config/             # YAML + env 配置 + providers.yaml + preflight
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

---

## 配置文件参考

#### `.env` — API key 和服务 URL

```bash
# 必填
DEEPSEEK_API_KEY=sk-your-key-here

# 语音合成（可选，当前默认 MiniMax）
MINIMAX_API_KEY=sk-your-key-here

# 其他可选
TAVILY_API_KEY=tvly-dev-...      # 网页搜索
OPENAI_API_KEY=sk-...            # 备选 LLM / STT / Embedding
MIMO_API_KEY=sk-...              # 图片识别
```

完整模板见 `.env.example`。

#### `config.yaml` — 主配置

```yaml
agent:
  character: character/elysia.yaml  # 角色文件路径
  user_name: 妖梦                    # 用户称呼

llm:
  model: deepseek/deepseek-v4-flash  # 可换 openai/gpt-4o、ollama/llama3 等

server:
  host: 0.0.0.0
  port: 8765
  image_max_mb: 10                  # 图片上传上限 (MB)

tts:
  enabled: true
  model: minimax/speech-2.8-hd      # 可换 gpt-sovits/default、custom/...

stt:
  enabled: true
  model: sensevoice/sense-voice      # 可换 custom/...

wake_word:
  enabled: false                     # 语音唤醒（需额外模型）

live2d:
  static_image: "/static/Elysia.png"

vision:
  model: mimo/mimo-v2.5              # describe_image 工具用的视觉模型
```

#### `character/elysia.yaml` — 角色人设

```yaml
name: Elysia
voice: elysia_v2
personality:
  traits: [温柔, 好奇, 偶尔毒舌]
  speaking_style: "口语化，短句为主，偶尔加语气词，喜欢用「呢」「呀」「哦」"
emotion:
  thresholds:
    excitement: 0.6
    sadness: 0.3
wake_words: [Elysia, 爱莉, 爱莉希雅, 妖精小姐]
proactive:
  enabled: false
  interval: 300                     # 主动搭话间隔（秒）
```

#### `Module/tts/minimax_voices.yaml` — MiniMax 音色

```yaml
Elysia:
  voice_id: "elysia_test_001"
```

`voice_id` 需要在 [MiniMax 开放平台](https://platform.minimaxi.com) 获取：
- **系统音色**（免费）— 官网「声音管理」中复制 voice_id，或调用 `POST /v1/get_voice` 接口查询
- **克隆音色** — 上传 10 秒~5 分钟音频样本，调用 `/v1/voice_clone` 接口（需企业/个人认证）
- **AI 生成** — 通过文本描述生成新音色，调用 `/v1/voice_design` 接口
- 完整 API 文档见 `Module/tts/minimax_tts_api.md`

#### `Module/tts/gpt_sovits_voices.yaml` — GPT-SoVits 音色

```yaml
Elysia:
  ref_audio: "/path/to/reference.wav"
  prompt_text: "参考音频对应的文本"
```

> `Module/config/providers.yaml` 和 `docker-compose.yml` 是内部默认值，通常不需要改。
