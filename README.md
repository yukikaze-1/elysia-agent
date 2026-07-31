# Elysia Agent

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Linux](https://img.shields.io/badge/platform-Linux-orange.svg)](#)

桌面 AI 伴侣，支持语音对话、内心独白、工具调用和后台任务。

> **运行环境：Linux**（Windows/Mac 用户请使用 WSL2 或虚拟机）

> 最近 (2026-07-03): Brain Prompt 提供者架构——12 个 PromptSection 独立模块，自管数据、自管模板。2026-06-26: Module/_common/ 轻量共享层重构——消除 6 个 registry + 5 个 test() + 9 个 __main__ + 6 个 test_config 重复代码，971 测试零回归。

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
- **[Node.js](https://nodejs.org/en/download)** — 前端构建需要（>= 18）。装好后输入 `node --version` 和 `npm --version` 确认
- **[DeepSeek API key](https://platform.deepseek.com/api_keys)** — 免费注册，在左侧「API Keys」页面点"创建 API key"，复制备用

> 系统自带 Python >= 3.11 可替代 conda（建议配合 `python3 -m venv .venv` 使用，避免依赖污染系统环境）

### 3. 首次安装（只需一次）

```bash
# 安装环境 + 下载模型（约 1.3GB，需网络）
bash scripts/setup.sh

# 编辑 .env，填入 API key（nano .env，或用桌面文本编辑器打开）
#   必填: DEEPSEEK_API_KEY
#   语音: MINIMAX_API_KEY（TTS，免费注册 https://platform.minimaxi.com）
# 更多可选配置见 .env.example 中的注释

# 构建前端页面（Vue 3 + Vite）
cd frontend && npm install && npm run build && cd ..

# 启动数据库（Docker 容器，重启后自动保持运行）
docker compose up -d
```

### 启动（每次使用）

```bash
bash scripts/start.sh
```

自动完成：conda 激活 → 数据库检查 → 快速预检 → 启动服务（WebSocket 服务器）。

打开浏览器访问 **`http://localhost:8765`**，在聊天界面与 AI 对话。日志输出到 `log/` 目录（主日志 `app.log`，全量 `app-debug.log`。详见 `log/README.md`）。停止：`Ctrl+C`。

Debug 面板：**`http://localhost:8765/debug`** — 内部状态监控仪表板，详见 [`static/debug.md`](static/debug.md)。

### 关键文件

| 文件/目录 | 用途 |
|-----------|------|
| `log/app.log` | 应用主日志 — INFO+ 关键事件 |
| `log/app-debug.log` | 全量调试日志 — DEBUG+ |
| `log/conversation.log` | 所有对话记录 |
| `log/mongo.log` | MongoDB 操作日志 |
| `log/milvus.log` | Milvus 操作日志 |
| `log/README.md` | 日志文件详细说明 |
| `.model_cache/` | 嵌入模型（`BAAI/bge-large-zh-v1.5/`，约 1.3GB） |
| `data/ref/Elysia.wav` | GPT-SoVits 音色克隆参考音频（1.6MB） |
| `frontend/` | Vue 3 前端源码（Vite + Pinia + TypeScript，含 Debug 应用）。详见 [frontend/README.md](frontend/README.md) |
| `static/` | Vite 构建产物 + 非代码资源（由 aiohttp serve） |
| `static/live2d-demo/` | Live2D 前端（Cubism SDK，index.html 待创建） |
| `data/live2d-assets/` | Live2D 角色模型（.moc3 + 纹理，服务端映射到 `/live2d/`） |
| `static/debug.md` | Debug 面板完整文档（标签页、API、技术实现） |

> `scripts/download_model.py` 将模型下载到 `.model_cache/`。运行时按 `MODEL_CACHE_DIR` → `.model_cache/` → `~/.cache/huggingface/hub/` 顺序查找。

### 高级用法（可选）

```bash
python app.py --cli              # 交互终端模式（stdin 打字）
bash scripts/check.sh           # 环境预检（推荐，自动检测 conda/python3）
bash scripts/check.sh --full    # 含连通性探活
python app.py --check            # 同上（需手动激活 conda 环境）
python app.py --check --full     # 同上
python app.py --host 0.0.0.0 --port 8080  # 自定义端口
python app.py --verbose          # 详细日志（调试用）
python app.py --config custom.yaml --character custom/char.yaml  # 自定义配置
```

### 前端开发（可选）

首次使用前需要构建前端（`bash scripts/setup.sh` 会自动完成）。修改 `frontend/` 源码后需要重新构建。

```bash
cd frontend
npm install                     # 首次：安装依赖（Vue 3 + Vite + Pinia + vis-network）
npm run dev                     # 开发模式：热更新服务器 :5173（自动代理到 :8765）
npm run build                   # 生产构建：输出到 ../static/
```

构建后刷新浏览器即可看到变更。前端技术栈：Vue 3 + Vite + Pinia + TypeScript + vis-network。

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
bash scripts/check.sh --full    # 完整预检，准确定位问题
python app.py --check --full  # 同上（需手动激活 conda 环境）
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

三层结构：

```
L0  Module/              无状态原子能力 — LLM, STT, TTS, Embedding, Database, Output, _common/, system_prompt, tool
L1  core/                认知中枢 — EventBus → AgentFSM → CognitiveLoop → PromptAssembler
L2  core/memory/         记忆与工具 — SessionMemory, LongTermMemory, ToolAdapter (Plugin 架构)
L2  Module/task_system/  后台任务 — Planner → Executor → Evaluator (DAG 编排)
```

每层职责分明。每个 Module 服务遵循 Protocol-Registry-Backend 模式，可独立替换。

数据流：用户输入 → EventBus → FSM(THINKING) → CognitiveLoop(inner_voice/TTS/tool_calls) → IDLE（详见 [docs/architecture.md](docs/architecture.md) 完整管线）。

## 文档导航

### 用户文档
| 文档 | 内容 |
|------|------|
| [快速开始](#快速开始) | 安装、启动、配置、故障排查 |
| [docs/README.md](docs/README.md) | docs/ 文档索引 |
| [docs/testing.md](docs/testing.md) | 测试命令速查 + 分层体系 |
| [docs/legacy/companion-roadmap.md](docs/legacy/companion-roadmap.md) | 伴侣化改造路线（⚠️ 已废弃，保留作历史参考） |
| [docs/legacy/companion-prompt-guide.md](docs/legacy/companion-prompt-guide.md) | Prompt 编写指南（⚠️ 已废弃，保留作历史参考） |

### Module 层
| 模块 | README | 说明 |
|------|--------|------|
| LLM | [Module/llm/README.md](Module/llm/README.md) | 大模型调用（DeepSeek/OpenAI/Ollama） |
| STT | [Module/stt/README.md](Module/stt/README.md) | 语音识别（SenseVoice） |
| TTS | [Module/tts/README.md](Module/tts/README.md) | 语音合成（GPT-SoVits/MiniMax） |
| Embedding | [Module/embedding/README.md](Module/embedding/README.md) | 文本向量化（HuggingFace/OpenAI） |
| Output | [Module/output/README.md](Module/output/README.md) | 多通道输出（Console + WebSocket） |
| Config | [Module/config/README.md](Module/config/README.md) | 配置基础设施（YAML + env + 校验） |
| Logger | [Module/logger/README.md](Module/logger/README.md) | 日志工厂（plain + JSON） |
| _common | [Module/_common/README.md](Module/_common/README.md) | 共享工具层（env + registry_helpers + test 工厂） |
| System Prompt | [Module/system_prompt/README.md](Module/system_prompt/README.md) | System Prompt 引擎 + Section Provider |
| TaskSystem | [Module/task_system/README.md](Module/task_system/README.md) | 后台任务委派与 DAG 编排 |
| Tool | [Module/tool/README.md](Module/tool/README.md) | 工具执行框架（Plugin + SafetyPipeline） |
| Database | [Module/database/README.md](Module/database/README.md) | 数据库总览（Milvus + MongoDB） |
| VectorDB | [Module/database/vectordb/README.md](Module/database/vectordb/README.md) | 向量数据库（Milvus） |
| DocumentDB | [Module/database/documentdb/README.md](Module/database/documentdb/README.md) | 文档数据库（MongoDB） |
| Bench | [Module/bench/README.md](Module/bench/README.md) | 延迟基准测试 |
| Module 总览 | [Module/README.md](Module/README.md) | 架构模式 + 各模块 API 速览 |

### Core 层
| 模块 | README | 说明 |
|------|--------|------|
| Core 总览 | [core/README.md](core/README.md) | 认知中枢架构 + 组件详解 |
| Input | [core/input/README.md](core/input/README.md) | AudioBuffer + WebSocket 服务器 + Debug API |
| Agent | [core/agent/README.md](core/agent/README.md) | 组装入口 + 生命周期管理 |
| CognitiveLoop | [core/cognitive/README.md](core/cognitive/README.md) | ReAct 调度器 + StreamProcessor 流式解析 |
| Memory | [core/memory/README.md](core/memory/README.md) | 记忆子系统（Session + LTM + EmbeddingProvider + 沉淀 + 去重） |
| Monitoring | [core/monitoring/README.md](core/monitoring/README.md) | 运行时可观测性（EventBus + IdleMonitor + TokenTracker） |
| Debug | [core/debug/README.md](core/debug/README.md) | 调试仪表板数据层（16 panel + 录制） |
| Text | [core/text/README.md](core/text/README.md) | 文本流处理管线（BlockFilter + SentenceBuffer） |
| Storage | [core/storage/README.md](core/storage/README.md) | 统一存储层（Repository 模式） |
| Schema | [core/schema/README.md](core/schema/README.md) | 基础类型层（AgentState/事件/多模态消息 Part） |
| Config | [core/config/README.md](core/config/README.md) | 应用配置 + 角色配置 dataclass |
| Adapter | [core/adapter/README.md](core/adapter/README.md) | 适配器层（LLM + TTS + Tool + Message） |
| Task | [core/task/README.md](core/task/README.md) | 任务数据模型 + Worker Pool 调度器 |
| Utils | [core/utils/README.md](core/utils/README.md) | 通用工具（message_id + log_setup） |
| CharacterEngine | [core/personality/README.md](core/personality/README.md) | 四层性格+情绪模型（Core→Self→State→Mood） |
| 工具总览 | [core/tools/README.md](core/tools/README.md) | 工具插件列表 + 开发指南 |
| 工具: filesystem | [core/tools/filesystem/README.md](core/tools/filesystem/README.md) | read_file / list_directory / write_file |
| 工具: memory | [core/tools/memory/README.md](core/tools/memory/README.md) | search_memory / save_memory |
| 工具: reminder | [core/tools/reminder/README.md](core/tools/reminder/README.md) | set_reminder |
| 工具: runtime_config | [core/tools/runtime_config/README.md](core/tools/runtime_config/README.md) | get_runtime_config |
| 工具: self_state | [core/tools/self_state/README.md](core/tools/self_state/README.md) | get_self_state |
| 工具: time | [core/tools/time/README.md](core/tools/time/README.md) | get_time |
| 工具: vision | [core/tools/vision/README.md](core/tools/vision/README.md) | describe_image（多模态视觉） |
| 工具: web | [core/tools/web/README.md](core/tools/web/README.md) | web_search / web_fetch |

### 其他文档
| 文档 | 内容 |
|------|------|
| [docs/architecture.md](docs/architecture.md) | 完整架构详解（1000+ 行） |
| [docs/frontend-api.md](docs/frontend-api.md) | 前端 API 文档 |
| [CLAUDE.md](CLAUDE.md) | AI 助手指令 + 项目地图 |
| [character/README.md](character/README.md) | 角色配置说明 |
| [core/prompts/README.md](core/prompts/README.md) | Prompt 模板说明 |
| [docs/database-storage.md](docs/database-storage.md) | 数据库存储设计 |
| [docs/sync-lessons.md](docs/sync-lessons.md) | 同步经验笔记 |
| [docs/experience.md](docs/experience.md) | 开发经验记录 |
| [scripts/README.md](scripts/README.md) | 工具脚本（启动/预检/清理数据库） |
| [docs/readme-conventions.md](docs/readme-conventions.md) | README 编写规范（模板 + 面包屑规则 + 检查清单） |
| [docs/data-structure-survey.md](docs/data-structure-survey.md) | 全项目数据结构普查（2026-07-15） |
| [docs/review/doc-review-2026-07-18.md](docs/review/doc-review-2026-07-18.md) | 文档审查报告（2026-07-18） |

---

## 伴侣化改造

| 步骤 | 状态 | 内容 |
|------|------|------|
| 🥇 对话风格 | ✅ | Brain Prompt 模块化——12 个独立 section，自然对话/克制/关系模型 |
| 🥈 内心世界 | ✅ | `<inner_voice>` 标签输出 + 前端气泡 + 持久化 + 日志 |
| 🥉 记忆人格化 | 待做 | 记忆融入对话，不是"引用"是"记得" |
| 🏅 主动性 | 待做 | 注意到离开/回来，深夜关心 |

详见 [`docs/legacy/companion-roadmap.md`](docs/legacy/companion-roadmap.md) 和 [`docs/legacy/companion-prompt-guide.md`](docs/legacy/companion-prompt-guide.md)

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
| **TaskSystem v1** — DAG 编排 + Planner/Executor/Evaluator (339 tests) | done |
| **Token 追踪** — TokenTracker + PromptRecorder + 前端标签 | done |
| 工具系统 Plugin 架构 — 12 插件 + SafetyPipeline + HITL 审批 | done |
| **模型切换** — DeepSeek/OpenAI/Ollama/MiMo + 运行时热切换 | done |
| **多模态** — 图片上传/预览/lightbox + describe_image 视觉工具 | done |
| Live2D 虚拟角色渲染 (Cubism SDK) | done |
| 前端 Vue 3 重构 (Vite + Pinia + TypeScript) | done |
| 配置系统 (YAML + env + 类型校验 + providers.yaml) | done |
| [/debug 面板](static/debug.md) (16+ 面板: FSM/记忆/工具/Token/DAG/LLM Prompt/Raw Response/数据库管理) | done |
| 优雅退出 + /health /ready 端点 | done |
| LLM 重试 (指数退避) + 请求超时 | done |
| 日志系统 — stderr + log/app.log + log/conversation.log + 库日志分离 | done |
| 会话恢复 (localStorage + 服务端持久化) | done |
| **一键部署** — setup.sh + start.sh + download_model.py | done |
| **环境预检** — app.py --check / --check --full (16 项：11 基础 + 5 连通性) | done |
| **Docker 数据库** — docker-compose.yml (MongoDB 7 + Milvus v2.4) | done |

---

## 项目结构

```
Agent/
├── app.py                  # 入口
├── config.yaml             # 总配置
├── requirements.txt        # Python 依赖
├── docker-compose.yml      # 数据库服务
├── Makefile                # 测试/检查/启动命令
├── pytest.ini              # pytest 配置（4 层 markers）
├── .env.example            # 环境变量模板
├── scripts/                # 安装/启动/清理工具
│   ├── setup.sh            #   一键安装
│   ├── start.sh            #   一键启动
│   ├── check.sh            #   环境预检
│   ├── download_model.py   #   嵌入模型下载
│   ├── clean_db.py         #   数据库清理
│   └── README.md           #   脚本说明
├── todo.md                 # 开发计划 + 已知 Bug
├── log/                    # 日志输出 (gitignore)
├── data/                   # 数据存储 (gitignore)
│   ├── cache/              #   缓存
│   ├── live2d-assets/      #   Live2D 角色模型
│   ├── ref/                #   参考音频
│   └── uploads/            #   上传文件
├── character/              # 角色配置
│   ├── elysia/             #   Elysia 角色
│   ├── cyrene/             #   Cyrene 角色
│   └── README.md           #   角色配置说明
│
├── Module/                 # L0 基础设施层
│   ├── _common/            #   共享工具 (registry_helpers, test_helpers, main_runner, ...)
│   │   └── tests/          #     测试
│   ├── llm/                # DeepSeek / OpenAI / Ollama / MiMo
│   ├── stt/                # SenseVoice / OpenAI-compatible
│   ├── tts/                # GPT-SoVits / MiniMax / OpenAI-compatible
│   ├── embedding/          # HuggingFace / OpenAI
│   ├── output/             # Console + WebSocket 多通道
│   ├── config/             # YAML + env 配置 + providers.yaml + preflight
│   ├── logger/             # 日志工厂
│   ├── system_prompt/      # Brain Prompt 引擎 — SystemPromptManager + 12 PromptSections
│   ├── tool/               # 工具框架 (129 tests)
│   ├── database/           # Milvus / MongoDB / GraphDB
│   ├── task_system/        # TaskSystem v1 (339 tests)
│   │   └── tests/          #     测试
│   └── bench/              # 延迟基准
│
├── core/                   # Elysia Agent 应用程序
│   ├── agent/               # Agent (package): 主 agent + 生命周期 + server
│   ├── cognitive/           # CognitiveLoop: ReAct 调度器 + inner_voice 状态机
│   ├── adapter/             # 适配层: LLM + TTS + tool_call_parser
│   ├── text/                # 文本流处理 (BlockFilter + SentenceBuffer)
│   ├── debug/               # Debug 面板数据层
│   ├── input/               # 输入层: AudioBuffer + WebSocket server + debug API
│   ├── memory/              # 记忆子系统 (Session + LTM + EmbeddingProvider + Consolidator + Reflector)
│   ├── monitoring/          # 可观测性: EventBus + IdleMonitor + TokenTracker
│   ├── personality/         # CharacterEngine: 四层性格模型
│   ├── schema/              # 共享 Pydantic 类型 (Event, Message, Part 等)
│   ├── storage/             # Repository 层 (Session/Message/Token/Memory/CharacterVersion)
│   ├── tools/               # 工具插件 (filesystem, web, memory 等)
│   ├── tests/               # 测试套件
│   ├── config/               # 配置子包 (AppConfig + CharacterConfig)
│   ├── utils/                  # 通用工具 (message_id + log_setup)
│   ├── task/                  # 任务子系统 (Task 数据模型 + TaskScheduler Worker Pool)
│   ├── prompts/              # Prompt 模板 (Jinja2 .j2)
│   │   ├── ExtractMemory.j2  #   对话提取记忆
│   │   ├── ConsolidationCompare.j2 # 记忆比对
│   │   ├── MemoryReflector.j2 #  记忆去重反思
│   │   ├── CharacterReflector.j2 # 角色进化反射
│   │   ├── PersonalityCompression.j2 # 性格压缩
│   │   ├── AwarenessJudge.j2  #  C9 觉察判定
│   │   ├── ActiveSpeak.j2    #   主动说话判断
│   │   ├── Amygdala.j2       #   情绪分析
│   │   ├── SpeakingStyleEdit.j2 # C6 风格编辑
│   │   ├── Utils.j2          #   通用工具宏
│   │   ├── MacroReflector.j2 #   宏观反思 (参考)
│   │   ├── MicroReflector.j2 #   微观反思 (参考)
│   │   ├── ref/              #   GPT 生成参考
│   │   └── README.md         #   模板说明
│   └── __init__.py          # 包标记 (空)
│
├── frontend/                 # Vue 3 前端源码（Vite + Pinia + TypeScript）
│   ├── index.html              # 聊天页面入口
│   ├── debug.html              # Debug 面板入口
│   ├── src/main.ts             # 聊天应用入口（Pinia + marked + highlight.js）
│   ├── src/App.vue             # 根组件（三栏布局 + 全局快捷键）
│   ├── src/components/         # 21 个聊天 Vue 组件
│   ├── src/stores/             # 6 个 Pinia store
│   ├── src/composables/        # 3 个 composables
│   ├── src/types/protocol.ts   # WebSocket 消息类型定义
│   ├── src/styles/variables.css # 全局 CSS 变量
│   └── src/debug/              # 独立 Debug 应用（Vite 多入口）
│       ├── main.ts             #   Pinia 注入入口
│       ├── App.vue             #   Tab shell（概览 | 数据库 | 任务）
│       ├── types.ts            #   DebugData TS 接口（40 字段）
│       └── components/         #   14 个 Debug 组件
│           ├── OverviewTab.vue   #   16 面板 2 列 grid
│           ├── DatabaseTab.vue   #   DB CRUD 容器
│           ├── LtmPanel.vue      #   LTM 增删改查 + 翻页
│           ├── SessionPanel.vue  #   会话管理
│           ├── ArchivePanel.vue  #   消息归档
│           ├── TasksTab.vue      #   SSE 任务流 + vis-network DAG
│           ├── TaskCard.vue      #   任务卡片
│           ├── shared/           #   可复用 UI
│           └── modals/           #   Prompt/Response/VersionHistory/ImageLightbox
│
├── static/                 # 静态资源 + Vite 构建产物（由 aiohttp serve）
│   ├── index.html          #   聊天页面 HTML（Vite 构建产物）
│   ├── debug.html          #   Debug 页面 HTML（Vite 构建产物）
│   ├── debug.md            #   Debug 面板设计文档
│   ├── Elysia.png          #   角色头像（非代码资源）
│   ├── live2d-demo/        #   Live2D Cubism SDK（非代码资源）
│   └── assets/             #   Vite 构建产物（JS/CSS bundle，带 content hash）
├── docs/                   # 文档
│
└── log/                    # 日志 (gitignore)
```

---

## 测试

1927 个测试按 4 层 markers 组织。详见 [docs/testing.md](docs/testing.md)。

```bash
conda activate Elysia

# 日常开发 — 秒级（~60s）
make test-quick

# 提交前 — 排除慢速（~5min）
make test-normal

# 全量（~22min）
make test-full
```

| marker | 标准 | 用途 |
|--------|------|------|
| `unit` | 纯函数/类，无 I/O | 日常开发 |
| `service` | 单模块 + mock | 日常开发 |
| `integration` | 需真实 DB 或 API | 提交前 |
| `slow` | Agent 启动/端到端 | 推之前 |

---

## Live2D 角色显示（可选）

项目默认以静态图片代替。启用动态 Live2D 角色需要两步：

**1. 获取 Cubism SDK for Web**

从 [Live2D 官网](https://www.live2d.com/download/cubism-sdk/) 下载 `CubismSdkForWeb-5-r.5.zip`，解压后放入 `static/live2d-demo/`：

```
static/live2d-demo/
├── Core/
│   └── live2dcubismcore.js  # 核心渲染引擎
├── Framework/
│   └── dist/                  # 编译好的 TypeScript 框架
├── Samples/                   # 官方示例模型（可选）
└── index.html                 # 你的 Live2D 加载页面
```

> 当前 `static/live2d-demo/` 已包含 SDK 的 `Core/` + `Framework/`，但缺 `index.html` 加载页面

**2. 获取角色模型**

从 [Live2D 商城](https://www.live2d.com/en/products/) 下载角色模型包，放入 `data/live2d-assets/`：

```
data/live2d-assets/
└── <角色名>/
    ├── <角色名>.moc3            # 模型数据
    ├── <角色名>.model3.json     # 模型配置（入口文件）
    ├── <角色名>.physics3.json   # 物理模拟
    └── <角色名>.8192/           # 纹理贴图
```

服务端将 `data/live2d-assets/` 映射到 `/live2d/`，前端通过 iframe 加载 `/static/live2d-demo/index.html`（需自行创建加载页面）。

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
  character: character/elysia/config.yaml  # 角色文件路径
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

#### `character/<name>/config.yaml` — 角色配置

每个角色一个子目录。以 Elysia 为例 (`character/elysia/config.yaml`):

```yaml
name: Elysia
user_pronoun: 她
base_personality_file: personality.md    # L1 Core 文本（相对路径）
voice: elysia_v2                         # [占位] TTS voice ID

emotion:
  thresholds:
    excitement: 0.6
    sadness: 0.3
  idle_expressions: [neutral, blink]

wake_words: [Elysia, 爱莉, 爱莉希雅, 妖精小姐]

proactive:
  enabled: false
  interval: 300
  triggers:
    - type: silence_timeout
      duration: 30
    - type: time_of_day
      times: ["08:00", "12:00", "22:00"]
```

#### `character/<name>/personality.md` — L1 Core 性格

自然语言撰写的核心性格描述，由 `base_personality_file` 指向，启动时加载。

```txt
温柔是你的底色——但不是那种软绵绵的"什么都好"的温柔。
你温柔是因为你在乎，不是因为你没脾气。
...
```

性格演化由 `core/personality/` 下的 CharacterEngine 管理（四层模型: Core→Self→State→Mood）。详见 [core/personality/README.md](core/personality/README.md)。

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
