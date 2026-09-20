# Elysia Agent

[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Linux](https://img.shields.io/badge/platform-Linux-orange.svg)](#)

桌面 AI 伴侣后端：语音对话、内心独白、长期记忆、工具调用与后台任务。

> **运行环境：Linux**（Windows/Mac 用户请使用 WSL2 或虚拟机）。
> **版本权威**：Python **3.12**、Node **22.x**（LTS）——详见
> [docs/operations.md §11](docs/operations.md)。

---

## What it is

三个顶层包，职责分明（**系统级权威是 [docs/architecture.md](docs/architecture.md)**，
本节只是能让你读懂目录的一句话概要）：

| 包 | 是什么 |
|---|---|
| `Module/` | **能力与基础设施**：LLM / STT / TTS / Embedding / 向量库 / 文档库 / 输出通道 / 配置 / system prompt / 工具框架 / 后台任务引擎。每个服务遵循 `protocol → registry → backend` 模式，可独立替换 |
| `core/` | **应用与领域编排**：`Agent`（组合根，通过 **46 个同名 `@property`** 公开子系统）、认知循环、角色引擎、记忆、存储仓储、输入层（WS/HTTP）、工具插件 |
| `frontend_contract/` | **共享 wire 契约**：可执行 Pydantic 模型 + 生成器 + 漂移门禁（零依赖，不 import 上面两者） |

数据流一句话：用户输入 → EventBus → 状态机（THINKING）→ 认知循环（inner_voice / TTS / 工具调用）→ IDLE。

---

## Requirements

| 需要什么 | 说明 |
|---|---|
| [Miniconda](https://docs.anaconda.com/miniconda/install/) | Python 环境管理（项目实际构建的是 **3.12**；conda env 名 `Elysia`） |
| [Docker Engine](https://docs.docker.com/engine/install/ubuntu/) | 自带 Compose 插件；跑 MongoDB + Milvus |
| [Node.js](https://nodejs.org/en/download) **22.x** | **仅**构建 v1 网页 UI 时需要（backend-only 可省略）。CI 钉 22.23.0 并断言主版本 |
| [DeepSeek API key](https://platform.deepseek.com/api_keys) | 唯一必填的外部凭据 |
| 可选：MiniMax key / Tavily key / MiMo key | TTS / 网页搜索 / 图片识别；不配则对应功能降级或跳过 |

> 系统自带 Python 也可替代 conda（建议 `python3 -m venv .venv`），但**只有 3.12 是被 lock
> 与 CI 验证过的版本**——预检与 `setup.sh` 的兜底分支接受 `>= 3.11`，那是下界判据。
>
> 依赖单源：`environment.yml`（声明）→ `environment.lock.yml`（精确锁）。
> `conda env create -f environment.lock.yml` 即得与 CI 一致的环境；
> 增删依赖后跑 `make lock-env`，`make check-env-lock` 是漂移门禁。

---

## Quick Start

```bash
git clone <仓库地址> && cd elysia-agent

# 1) 安装环境 + 下载嵌入模型（~1.3GB，需网络）
bash scripts/setup.sh                    # backend-only（默认，无需 Node）
bash scripts/setup.sh --with-frontend    # 需要 v1 网页 UI 时

# 2) 编辑 .env 填 key（必填 DEEPSEEK_API_KEY；TTS 用 MINIMAX_API_KEY）
#    模板与逐项说明见 .env.example

# 3) 启动数据库（重启后自动保持运行；setup.sh 已写入随机密码）
docker compose --profile dev up -d
```

## Run

```bash
bash scripts/start.sh                    # 推荐：单实例守卫 + 数据库探测 + 预检 + 启动
bash scripts/start.sh --with-frontend    # 额外构建 v1 UI
bash scripts/start.sh --skip-preflight   # 显式跳过预检（降级，慎用）
make start                               # ≡ bash scripts/start.sh
```

启动后：

| 入口 | 说明 |
|---|---|
| `http://localhost:8765/ws` | WebSocket 主入口（Electron / 前端连这个） |
| `http://localhost:8765/health` `/ready` | liveness（恒 200）/ readiness（依赖不 ok → 503） |
| `http://localhost:8765/debug` | Debug 仪表盘（需已构建前端；数据面 `/debug/api/*` 需 `SERVER_AUTH_TOKEN`）。使用说明见 [static/debug.md](static/debug.md) |
| `http://localhost:8765/` | v1 网页聊天——**默认停用**，`SERVE_CHAT=1` 恢复 |

其他入口：`python app.py --cli`（终端交互）、`python app.py --check [--full]`（只预检，不启动）、
`python app.py --host/--port …`。

> **非回环绑定是 fail-closed 的**：未配 `AGENT_ACCESS_TOKEN` 时拒绝启动（除非显式
> `ALLOW_INSECURE_REMOTE=1`）。开发期远程测试的完整做法与风险见
> [docs/operations.md §1.4](docs/operations.md)。

## Frontend

Vue 3 + Vite + Pinia + TypeScript。开发时 Vite（:5173）代理 WS/API 到 :8765；生产构建直接输出到 `static/`。

```bash
cd frontend
npm ci            # 严格按 lock（CI 用同一条）
npm run dev       # 开发服务器 :5173
npm run build     # = vue-tsc --noEmit && vite build → ../static/
npm test          # vitest（store 层不变量）
```

> v1 聊天默认停用：构建产物就位后访问 `/` 仍需 `SERVE_CHAT=1`。
> Debug 仪表盘与聊天共享 `/static/assets/*`，同样需要先构建。

## Configuration（只列入口）

**优先级：显式 CLI 覆盖 > 环境变量 > YAML > 代码默认值。**

| 入口 | 放什么 | 权威 |
|---|---|---|
| `config.yaml` | 主配置（agent / llm / memory / server / tts / stt / wake_word / live2d / database / vision） | 字段与默认值在 `core/config/app_config.py` |
| `.env` | 凭据与端点（另见 [.env.example](.env.example)） | 变量语义见 [docs/operations.md §3](docs/operations.md) |
| `Module/config/providers.yaml` | provider 注册表 + 端点/能力位/窗口默认值 | [Module/config/README.md](Module/config/README.md) |
| `character/<name>/config.yaml` | 单角色配置 | [character/README.md](character/README.md) |

最常见的三个键：

```yaml
agent:
  character: character/elysia/config.yaml   # 角色
llm:
  model: deepseek/deepseek-v4-flash         # <provider>/<model>
tts:
  model: minimax/speech-2.8-hd
```

> **运维面（启动/环境变量索引/数据库/provider/Ollama/健康/日志/备份边界/排障）的唯一权威是
> [docs/operations.md](docs/operations.md)。** 本文不复制字段清单——那正是最容易两处漂移的地方。

## Development

```bash
conda activate Elysia

# 日常迭代
make test-quick      # unit + service（-n 8）

# 提交前 —— 完整清单见 docs/testing.md §Required pre-push verification
make lint make pyright make check-types make check-arch make check-timeouts \
     make check-contract make check-debug-auth make check-ci-isolation \
     make check-secrets make check-test-sets make check-compose \
     make check-env-lock make audit make test-normal
cd frontend && npm ci && npm run build && npm test
```

> ⚠️ **`make check-full` 不是门禁聚合入口**——它是 `app.py --check --full`（环境预检）。
> **提交前该跑什么的唯一权威是 [docs/testing.md](docs/testing.md) 的
> 「Required pre-push verification」**；本文上面那行只是它的快捷摘录，可能与 CI 演进而不同步。
>
> 写代码前先读 [docs/code-style-guide.md](docs/code-style-guide.md)；
> 改 CI 前先读 [docs/ci.md](docs/ci.md)（CI 的唯一权威，含"为什么全部 self-hosted"与 PR 恢复清单）。

## Documentation

**入口是 [docs/README.md](docs/README.md)**（所有长期文档的导航与 authority map）。
最常用的几张：

| 我想… | 读 |
|---|---|
| 理解架构 | [docs/architecture.md](docs/architecture.md) |
| 知道"哪条断言会被门禁抓住" | [docs/architecture/invariants.md](docs/architecture/invariants.md) |
| 跑测试 / 提交前必跑什么 | [docs/testing.md](docs/testing.md) |
| 运维 / 排障 / 环境变量 | [docs/operations.md](docs/operations.md) |
| 接前端（wire 契约） | [docs/electron-backend-protocol-v2.md](docs/electron-backend-protocol-v2.md) + [frontend_contract/README.md](frontend_contract/README.md) |
| 存储设计 | [docs/database-storage.md](docs/database-storage.md) |
| 还剩什么没做 / 已经定了什么 | [todo.md](todo.md) / [docs/decisions.md](docs/decisions.md) |
| 以前发生过什么（历史，非当前事实） | [docs/hardening-program.md](docs/hardening-program.md) · [docs/review/README.md](docs/review/README.md) |
| 给 Coding Agent 的约定 | [CLAUDE.md](CLAUDE.md) |

## Troubleshooting（简版）

| 症状 | 处置 |
|---|---|
| 端口被占 | `lsof -i :8765`，或 `python app.py --port 8766` |
| Docker 起不来 | `docker compose --profile dev ps` / `logs milvus` / `restart` |
| "required variable … is missing a value" | `.env` 缺数据库密码 → `bash scripts/setup.sh` 或按 `.env.example` 手填 |
| 预检报错定位不到 | `bash scripts/check.sh --full` |
| 嵌入模型下载失败（代理/镜像） | `python scripts/download_model.py`；TUN 透明代理与 `huggingface_hub` 的坑见 [docs/archive/elysia-install-issues.md](docs/archive/elysia-install-issues.md) |
| 无 AVX 的旧 CPU 上 `mongo:8` 无限重启（exit 132） | compose 里把 image 换成 `mongo:4.4` |

> 完整排障表（httpx 的 `socks://`、Ollama 窗口、索引选项冲突、`conda run` 吞 SIGTERM、
> 日志分流、鉴权域…）见 [docs/operations.md §10](docs/operations.md)。
