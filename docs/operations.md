# 运维手册（Operations）

> 文档导航：[项目文档](../README.md) → [docs 索引](README.md) → 本文

本文是**启动、配置、依赖服务、健康、日志与恢复边界**的操作权威。
它不重复字段清单：配置 schema 的唯一权威是代码里的 dataclass 与
[`Module/config/README.md`](../Module/config/README.md)、[`.env.example`](../.env.example)。

**读法**：想知道"怎么把它跑起来" → §1；"某个开关怎么设" → §2/§3；
"数据库/模型服务怎么准备" → §4/§5/§6；"现在健康吗" → §7；"数据怎么保" → §9。

---

## 1. 启动

### 1.1 首次安装

```bash
bash scripts/setup.sh                       # 建 conda env + 下载嵌入模型（~1.3GB）
bash scripts/setup.sh --with-frontend       # 需要 v1 网页 UI 时（需 Node）
# 然后按 .env.example 填 .env（必填 DEEPSEEK_API_KEY；TTS 用 MINIMAX_API_KEY）
docker compose --profile dev up -d          # 数据服务（MongoDB + Milvus + etcd + MinIO）
```

`setup.sh` 会自动把随机数据库密码写进 `.env`。数据库凭据在 compose 侧用 `${VAR:?}`
强制——缺变量时 compose **拒绝启动**（不是用默认密码兜底）。

### 1.2 启动（每次使用）

```bash
bash scripts/start.sh                 # 推荐：flock 单实例 + 数据库就绪探测 + 预检 + 启动
bash scripts/start.sh --with-frontend # 额外 npm ci && npm run build
bash scripts/start.sh --skip-preflight# 显式跳过预检（降级，慎用）
make start                            # ≡ bash scripts/start.sh
```

`scripts/start.sh` 做五件事，顺序有意义：

1. **flock 单实例守卫**（无 `flock` 时显式警告，不静默跳过）；
2. 解析 conda env `Elysia` 并**直接 exec 它的 python**——**不用 `conda run`**，
   因为 `conda run` 不转发 SIGTERM，`Agent.stop()` 的清理步骤永远不会执行；
3. 探测 19530（Milvus）/ 27017（Mongo）端口，未开则 `docker compose --profile dev up -d`；
4. 预检 `app.py --check`（失败即退出 1，除非显式 `--skip-preflight`）；
5. 清空 `ALL_PROXY`/`all_proxy`（httpx 不支持 `socks://`，会抛 `Unknown scheme for proxy URL`）
   然后 `exec python app.py`。

其他入口：

| 命令 | 用途 |
|---|---|
| `python app.py` | 直接启动 WS 服务器（不做预检/DB 探测；需先自己激活 env） |
| `python app.py --cli` | 交互式终端模式（stdin 打字，`run_interactive`） |
| `python app.py --config/-c <path>` | 换配置文件（默认 `config.yaml`） |
| `python app.py --host/--port …` | 覆盖绑定（见 §1.4 的安全守卫） |
| `python app.py --verbose/-v` | DEBUG 日志 |
| `python app.py --check` / `--check --full` | **只做环境预检**，不启动服务（§7.4） |
| `bash scripts/check.sh [--full]` | 同上（自动检测 conda/python3） |
| `python -m core.input` | 仅输入层的入口（`core/input/__main__.py`） |

### 1.3 端口与绑定

| 项 | 值 | 权威 |
|---|---|---|
| 默认绑定 | `127.0.0.1:8765` | `config.yaml::server`（`core/config/app_config.py::ServerConfig`） |
| 覆盖 | env `SERVER_HOST` / `SERVER_PORT`，或 `--host` / `--port` | 优先级：显式 CLI > env > YAML > dataclass 默认 |
| Vite 开发服务器 | `:5173`，代理 `/ws`、`/api`、`/health`、`/ready`、`/shutdown`、`/restart`、`/debug`、`/tools`、`/uploads`、`/live2d` 到 `:8765` | `frontend/vite.config.ts` |
| 生产静态产物 | `npm run build` 直接输出到 `static/`（`outDir: ../static`，`emptyOutDir: false`），由 aiohttp serve | 同上 |

**没有应用级 Dockerfile / systemd unit / 进程管理器**——Not currently provided by the project.
`docker compose` 的 `dev` / `production` 两个 profile **只针对数据服务**，
不带 `--profile` 的 `docker compose up -d` 什么都不会起（空 profile 即 fail-closed）。

### 1.4 远程绑定是 fail-closed 的

非回环绑定且未配置 `AGENT_ACCESS_TOKEN` 时启动被**拒绝**（`core/utils/server_guard.py::
assert_secure_bind`），除非显式 `ALLOW_INSECURE_REMOTE=1`。注意：

- `SERVER_AUTH_TOKEN` 单独存在**不**授权远程绑定（它是 admin/debug 域）；
- 浏览器 WS 握手不能带自定义头 ⇒ token 模式对 WS 不可用，开发期匿名远程请用防火墙/内网缓解；
- 细节与威胁模型见 [`electron-backend-security.md`](electron-backend-security.md)。

### 1.5 重启语义

`core/agent/server.py::run_agent_service` 是**进程内循环**：`/restart` 让它重新加载 YAML
并重建 `Agent` 实例。哪些配置真的在重启后生效，docstring 有明确记录：

| 重启后生效 | 重启后**不**生效（在 `Agent.__init__` 里被值拷贝） |
|---|---|
| `server.*` / `stt.*` / `tts.*` / `wake_word.enabled` / `agent.max_pending_messages` | `database.*` / `agent.character` / `agent.user_name` / `memory.*` / `llm.model` |

---

## 2. 配置入口

**优先级：显式 CLI 覆盖 > 环境变量 > YAML > dataclass 默认值。**

| 入口 | 作用 | 权威 |
|---|---|---|
| `config.yaml` | 主配置（10 个顶层块：`agent` / `llm` / `memory` / `server` / `tts` / `stt` / `wake_word` / `live2d` / `database` / `vision`） | `core/config/app_config.py` 的 dataclass（字段名、默认值、约束都在那里） |
| `.env` | 凭据与 URL（`.gitignore` 忽略）；`app.py` 用 `load_dotenv(override=False)` 加载 ⇒ **已导出的环境变量优先于 `.env`** | [`.env.example`](../.env.example) 是模板；**逐字段语义**见 `Module/config/README.md` |
| `Module/config/providers.yaml` | **provider 注册表单源**：有哪些 provider、默认 base_url、`url_key`/`env_key`、`local`、能力位（`supports_prefix`/`supports_multimodal`/`supports_thinking`）、`context_window`、模型下拉列表 | `Module/config/providers.py::load_provider_defaults`；各 `Module/<service>/registry.py` 的 `_BACKENDS` 由它装载 |
| `custom_providers.yaml` | 用户自定义 provider（`api_key` 走 Secret Store 或 `api_key_env`） | `core/providers/custom_providers.py` |
| `character/<name>/config.yaml` | **单角色**配置（路径由 `agent.character` 指向） | `core/config/character_config.py::CharacterConfig.from_yaml` |
| `paths.py::ProjectPath` | 所有项目文件系统路径的单一定义点；`ELYSIA_ROOT` 定根，`ELYSIA_PATH_<MEMBER>` 逐路径覆盖 | `paths.py` |

> ⚠️ **`character/<name>/config.yaml` 的 `appearance:` 块不被读取**——
> `CharacterConfig.from_yaml()` 明确不读它（它是设计意图记录，不是生效配置项）。
>
> ⚠️ **`config.yaml::database.milvus_token` 永不生效**（`secret=True` ⇒ loader 显式跳过
> YAML）。真凭据只走 env `MILVUS_TOKEN`。

---

## 3. 环境变量索引

两套机制并存（都从生产代码检索得到）：

1. **`AppConfig` 字段**：`Module/config/loader.py::ConfigLoader._override_from_env` 遍历
   dataclass 字段，env 名 = `ConfigField(env=…)` 显式值，否则 `字段路径.upper()`。
2. **直接 `os.getenv`**：注册表 helper（`Module/_common/registry_helpers.py`）、路径、
   嵌入模型、工具代理等。

### 3.1 `AppConfig` 表面（37 个；全部有默认值 ⇒ 全部可选）

| 变量 | 由谁读 | 必需？ |
|---|---|---|
| `AGENT_CHARACTER` | `AgentConfig.character`（角色配置文件路径） | 否（默认 `character/elysia/config.yaml`） |
| `AGENT_USER_NAME` | `AgentConfig.user_name` | 否 |
| `AGENT_MAX_PENDING_MESSAGES` | `AgentConfig.max_pending_messages` | 否 |
| `LLM_MODEL` | `LLMConfig.model`（`provider/model`） | 否（默认 `deepseek/deepseek-chat`） |
| `MEMORY_SESSION_MAX_MESSAGES` | `MemoryConfig.session_max_messages` | 否 |
| `MEMORY_LTM_MAX_ENTRIES` | `MemoryConfig.ltm_max_entries` | 否 |
| `MEMORY_LTM_MAX_AGE_DAYS` | `MemoryConfig.ltm_max_age_days` | 否 |
| `MEMORY_LTM_ENABLE_EVICTION` | `MemoryConfig.ltm_enable_eviction` | 否 |
| `MEMORY_LTM_EMBED_TIMEOUT` | `MemoryConfig.ltm_embed_timeout` | 否 |
| `SERVER_HOST` | `ServerConfig.host` | 否（默认回环） |
| `SERVER_PORT` | `ServerConfig.port` | 否（默认 8765） |
| `SERVER_AUTH_TOKEN` | `ServerConfig.auth_token`（**admin/debug 域**，secret） | 否* |
| `AGENT_ACCESS_TOKEN` | `ServerConfig.access_token`（产品 `ws`/`api` 域，secret） | 否* |
| `SERVE_CHAT` | `ServerConfig.serve_chat`（`1` 才让 `/` 提供聊天页） | 否 |
| `SERVER_MAX_WS_CONNECTIONS` | `ServerConfig.max_ws_connections` | 否 |
| `RATE_LIMIT_WS_PER_MIN` | `ServerConfig.rate_limit_ws_per_min`（0 = 关闭） | 否 |
| `RATE_LIMIT_MESSAGES_PER_MIN` | `ServerConfig.rate_limit_messages_per_min` | 否 |
| `RATE_LIMIT_PROVIDER_TEST_PER_MIN` | `ServerConfig.rate_limit_provider_test_per_min` | 否 |
| `RATE_LIMIT_UPLOAD_PER_MIN` | `ServerConfig.rate_limit_upload_per_min` | 否 |
| `RATE_LIMIT_SEARCH_PER_MIN` | `ServerConfig.rate_limit_search_per_min` | 否 |
| `RATE_LIMIT_TOOLS_PER_MIN` | `ServerConfig.rate_limit_tools_per_min` | 否 |
| `SERVER_MAX_REQUEST_BYTES` | `ServerConfig.max_request_bytes`（app `client_max_size`） | 否 |
| `SERVER_IMAGE_MAX_MB` | `ServerConfig.image_max_mb` | 否 |
| `SERVER_IMAGE_ALLOWED_TYPES` | `ServerConfig.image_allowed_types` | 否 |
| `ATTACHMENT_SIG_SECRET` | `ServerConfig.attachment_sig_secret`（HMAC 短签，secret） | 否* |
| `TTS_ENABLED` | `TTSConfig.enabled` | 否 |
| `TTS_MODEL` | `TTSConfig.model`（`provider/voice-model`） | 否 |
| `TTS_RATE_LIMIT_RPM` | `TTSConfig.rate_limit_rpm` → `TTSFactory.create` / 模型切换路径 | 否 |
| `STT_ENABLED` | `STTConfig.enabled` | 否 |
| `STT_MODEL` | `STTConfig.model` | 否 |
| `WAKE_WORD_ENABLED` | `WakeWordConfig.enabled` | 否 |
| `LIVE2D_STATIC_IMAGE` | `Live2DConfig.static_image` → `build_capabilities` → `server_info` | 否 |
| `VISION_MODEL` | `VisionConfig.model`（`describe_image` 用） | 否 |
| `MONGODB_URI` | `DatabaseConfig.mongodb_uri` | 否（默认 `mongodb://localhost:27017`；**实际部署必须设**） |
| `MILVUS_URI` | `DatabaseConfig.milvus_uri` | 否（默认 `http://localhost:19530`） |
| `MILVUS_TOKEN` | `DatabaseConfig.milvus_token`（secret；`VectorDBFactory` 也直接回退读它） | 否* |
| `DATABASE_DB_NAME` | `DatabaseConfig.db_name` | 否（默认 `default`） |

\* 带 `secret=True` 的四个（`SERVER_AUTH_TOKEN` / `AGENT_ACCESS_TOKEN` /
`ATTACHMENT_SIG_SECRET` / `MILVUS_TOKEN`）**只从 env 读**，YAML 里写不生效。
它们的"必需"取决于部署形态：远程绑定必须给 `AGENT_ACCESS_TOKEN`（§1.4）；
配置了 `AGENT_ACCESS_TOKEN` 时附件签名 fail-closed 需要 `ATTACHMENT_SIG_SECRET`；
Milvus 开授权后需要 `MILVUS_TOKEN`。

### 3.2 直接读取的变量

| 变量 | 由谁读 | 说明 |
|---|---|---|
| `ELYSIA_ROOT` | `paths.py::_detect_project_root` | 定项目根（默认 `paths.py` 所在目录） |
| `ELYSIA_PATH_<MEMBER>` | `paths.py::ProjectPath.resolve` | 逐路径覆盖（如 `ELYSIA_PATH_LOG`） |
| `<PROVIDER>_API_KEY` / `<PROVIDER>_URL` | `Module/_common/registry_helpers.py::{resolve_api_key,resolve_base_url}` | 键名来自 `providers.yaml` 的 `env_key`/`url_key`；**空串按未设置处理**（S2-H7） |
| `OPENAI_API_KEY` / `OPENAI_API_BASE` | embedding（`Module/embedding/openai_compat.py`、`core/memory/embedding_provider.py`） | OpenAI 嵌入后端 |
| `TAVILY_API_KEY` | `core/tools/web/plugin.py` | 网页搜索 |
| `DDGS_PROXY` / `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`（含小写） | `core/tools/web/plugin.py` | 代理发现；`ALL_PROXY=socks://` 会打挂 httpx（§10） |
| `ALLOW_INSECURE_REMOTE` | `core/utils/server_guard.py` | 显式允许匿名远程绑定（开发期） |
| `MODEL_CACHE_DIR` / `HF_HOME` / `HF_MIRROR` / `HF_ENDPOINT` | `Module/embedding/huggingface.py`、`model_manifest.py` | 嵌入模型缓存与镜像（镜像须显式 opt-in） |
| `TASK_SYSTEM_MODEL` | `Module/workflow_contract/infra.py::resolve_task_system_model` | 后台任务用的模型（默认 `deepseek-v4-pro`） |
| `CONDA_PREFIX` / `CUDA_VISIBLE_DEVICES` / `SSL_CERT_FILE` | `app.py` | 启动期设置（conda 证书路径、强制空 CUDA 可见列表） |
| `RUN_EXTERNAL_TESTS` / `RUN_REAL_MONGO_TESTS` | 测试层（`Makefile`、根 `conftest.py`） | 仅测试；`make test-external` / `make test-real-mongo` |
| `STAGE4_SMOKE_LIVE` / `STAGE_H_FIXTURES` | `scripts/stage4_smoke.py` / `scripts/dev/seed_stage_h_pinned.py` | 仅开发脚本 |

### 3.3 LEGACY：**不要**按这些变量配置（生产无调用者）

下面这些变量在 `Module/*/config.py` 里有声明（并有文档提到），但**生产代码不读它们**——
真正生效的是 `providers.yaml` 的 `env_key`/`url_key` 或 `AppConfig`：

| 变量 | 声明处 | 生产状态 |
|---|---|---|
| `LLM_API_BASE` | `Module/llm/config.py::LLMConfig.base_url` | **无生产调用者**——唯一消费者 `LLMFactory.from_config` 只被测试调用；请用 `DEEPSEEK_URL` / `OPENAI_URL` / `OLLAMA_URL` 等 |
| `LLM_API_KEY` | `Module/llm/config.py::LLMConfig.api_key` | 同上；请用 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `MIMO_API_KEY` … |
| `STT_API_KEY` | `Module/stt/config.py::STTConfig.api_key` | **无生产调用者**；请用 `CUSTOM_STT_API_KEY` / `OPENAI_API_KEY` / `DEEPGRAM_API_KEY` |
| `TTS_API_KEY` | `Module/tts/config.py::TTSConfig.api_key` | **无生产调用者**；请用 `MINIMAX_API_KEY` / `CUSTOM_TTS_API_KEY` |
| `TTS_API_URL` | `Module/tts/config.py::TTSConfig.base_url` | 该**声明**无生产消费者；同名 env **仅** `Module/bench` 的延迟基准读（`python -m Module.bench`）。请用 `MINIMAX_TTS_URL` / `GPT_SOVITS_URL` |

> 保留它们在 `Module/*/config.py` 里是既有代码状态；运维文档不再把它们列为配置手段。

---

## 4. 数据库

### 4.1 需要什么

| 服务 | 用途 | 默认端点 |
|---|---|---|
| MongoDB | 会话 / 归档 / token / 附件元数据 / 角色域 / 幂等 / 用户身份 | `mongodb://localhost:27017`（库名 `default`） |
| Milvus | 长期记忆向量库 | `http://localhost:19530` |
| etcd + MinIO | Milvus 的依赖（compose 自带） | 内部网络，无宿主端口（production profile） |

存储细节（集合、索引、TTL、GC、删除语义）的唯一权威是
[`database-storage.md`](database-storage.md)——本文只讲**怎么把它拉起来**。

### 4.2 拉起

```bash
docker compose --profile dev up -d          # 开发：Mongo 27017 + Milvus 19530/9091 + MinIO 9000/9001
docker compose --profile production up -d   # 生产：仅 Mongo 27017 + Milvus 19530（不发布 MinIO console / Milvus metrics）
docker compose --profile dev ps
docker compose --profile dev logs milvus
docker compose --profile dev restart
```

安全策略（由 `scripts/check_compose_security.py` 静态门禁）：

- 数据服务宿主端口**一律只绑 `127.0.0.1`**；服务间走内部 bridge 网络（`elysia-internal`）；
- 凭据全部从 `.env` 用 `${VAR:?}` 取（缺一个即拒绝启动），无内置默认密码；
- Mongo 以 `--auth` 启动并建 root 用户；Milvus 开 `authorizationEnabled` + `defaultRootPassword`；MinIO 用强随机 root 凭据；
- production profile **不发布** MinIO console 9001 / Milvus metrics 9091 等管理端口。

凭据变量与轮换方式见 [`.env.example`](../.env.example) 的数据库段与
[`credential-handling.md`](credential-handling.md)。

### 4.3 测试库（与生产库隔离）

```bash
docker compose -f docker-compose.test.yml up -d     # 127.0.0.1:27018，容器名 elysia-mongo-test
make test-real-mongo
docker compose -f docker-compose.test.yml down --remove-orphans
```

**无 volume（容器即数据）、无 auth**——这是刻意的测试专用取舍。这一层会 **drop 整个库**，
所以库名必须含 `test`（`tests/real_mongo/conftest.py` 有守卫）。

### 4.4 启动期的 schema 自检

启动步骤 `schema_drift`（紧随 `character_integrity`）**只读**比对"代码声明的索引"与
"库里的实况"：

- **孤儿索引**（库里有、代码不再声明）→ 显著 warning，并给出 `drop_index` 命令；**绝不自动删**；
- **索引选项冲突**（如库里是普通唯一索引、代码声明的是部分唯一索引）→ `option_conflicts` 报 error；
- 扫描失败 → 降级为"未扫描"，**绝不让启动失败**。

**本项目没有、也刻意不做通用迁移框架**：新增集合/索引/字段由每次 `connect()` 幂等执行的
`ensure_index` 覆盖；只有"删/改已有索引"需要人工用 `MongoDBBackend.drop_index()`。
索引清单与判据见 [`database-storage.md`](database-storage.md) §五。

---

## 5. Provider

模型标识是 `"<provider>/<model>"` 字符串（如 `deepseek/deepseek-v4-flash`）。
每个服务一个 `Module/<service>/registry.py`：`_BACKENDS` 从 `providers.yaml` 装载，
`Factory.create()` 负责解析 base_url / api_key / 实例化，并拒绝未实现的 provider。

| 服务 | 已注册 provider | 已注册但**未实现** | 具体 backend |
|---|---|---|---|
| `llm` | `deepseek` / `openai` / `ollama` / `gemini` / `mimo` / `custom` | `gemini` | `OpenAICompatBackend`（deepseek/openai/mimo/custom）、`OllamaBackend`（**native 协议**，§6） |
| `stt` | `sensevoice` / `openai` / `deepgram` / `custom` | `openai` / `deepgram` | `SenseVoiceBackend`、`OpenAICompatSTTBackend` |
| `tts` | `gpt-sovits` / `minimax` / `custom` | （无） | `GPTSoVitsBackend`、`MiniMaxTTSBackend`、`OpenAICompatTTSBackend` |
| `embedding` | `huggingface` / `openai` | （无独立常量；`create()` 末尾对未知 provider 抛 `NotImplementedError`） | `HuggingFaceBackend`、`OpenAICompatEmbeddingBackend` |

**"哪些 provider 可用"的唯一权威是各 registry 的 `Factory.is_implemented()`**——
preflight、`/api/providers`、debug 面板都问它，没有第二张静态表。

密钥/端点解析顺序（`Module/_common/registry_helpers.py`）：

```
resolve_base_url:  显式传参 > env（空串视为未设置）> providers.yaml
resolve_api_key:   显式传参 > env > providers.yaml 的 default_api_key
```

按 provider 的具体 env 名见 `providers.yaml` 的 `env_key` / `url_key` 字段
（例：`DEEPSEEK_API_KEY`/`DEEPSEEK_URL`、`OPENAI_API_KEY`/`OPENAI_URL`、
`MIMO_API_KEY`/`MIMO_URL`、`MINIMAX_API_KEY`/`MINIMAX_TTS_URL`、`GPT_SOVITS_URL`、
`SENSEVOICE_URL`、`CUSTOM_*`）。

`context_window` 是**预算来源**（`= 值 × 0.75`，未声明用保守默认 32768），
**对 Ollama 同时是运行期分配请求**（§6）。

---

## 6. Ollama

### 6.1 为什么它有自己的 backend

Ollama 的 runtime 语义与通用 OpenAI 兼容协议**不可通约**：`/v1/chat/completions`
会**静默忽略** `options.num_ctx`（0.24.0 实测：带 `num_ctx=8192` 仍按 4096 跑），
而 native `/api/chat` 会照做。因此 `LLMFactory.create` 按 provider 名选择
`OllamaBackend`——**走 native `POST /api/chat`**，不做协议回退。

不变量的完整清单见 [`architecture/invariants.md`](architecture/invariants.md) §3.4（OL1–OL6）。

### 6.2 接入步骤

1. **安装并启动 Ollama**（本机或局域网内可达的 daemon）——**具体安装步骤本项目不提供**，
   请按上游文档操作；
2. **拉取模型**：如 `ollama pull qwen2.5:3b`；
3. **配置 Elysia**：`llm.model: ollama/qwen2.5:3b`（或 env `LLM_MODEL`）；
   端点默认 `http://localhost:11434/v1`（`providers.yaml::llm.ollama.base_url`，
   可被 `OLLAMA_URL` 覆盖；backend 会剥掉 `/v1` 或 `/api` 后缀得到 native 根）；
4. **无需 API key**：`providers.yaml` 里 `ollama` 的 `env_key` 为空；preflight 直接
   `_pass("LLM env ok (ollama 无需 API key)")`；backend 兜底用字面 key `"ollama"`；
5. **（可选）声明窗口**：`providers.yaml::llm.ollama.context_window`。

### 6.3 `context_window` / `num_ctx` 的真实语义

| 事实 | 说明 |
|---|---|
| 单一来源 | `providers.yaml::llm.ollama.context_window`——**同一个值**既是 prompt 预算来源，也是 native 请求的 `options.num_ctx` |
| 唯一解析点 | `Module/llm/registry.py::declared_context_window`（int/float/数字字符串 > 0 ⇒ int；`None`/≤0/非数字/bool ⇒ `None` + warning） |
| 何时下发 | **只在显式声明（> 0）时**下发。缺失/非法 ⇒ **不发** `num_ctx`。通用预算回退 32768 是**预算兼容行为**，绝不会变成分配指令（实测 `num_ctx=0/-1` 会得到 4 token 的畸形 runner） |
| 上限 | Ollama 自身会夹取（实测 `100000000` → `32768`）；Elysia 不新增版本相关的硬编码上限 |
| 超窗行为 | **HTTP 200 + 静默截断**（"丢掉中间"，保留前 `num_keep` 与最近内容）⇒ 人设/系统提示最先劣化。**永远不要**把"估算装得下"当作"provider 完整消费" |

### 6.4 诊断

`OllamaBackend.probe_runtime_context()` 会返回
`{configured, requested, observed, model, error}`（`observed` 读 `GET /api/ps context_length`，
失败不抛异常）。**注意：目前没有把它暴露出来的命令/路由/面板**——
an Ollama runtime-context diagnostics CLI is **Not currently provided by the project.**
当前只有测试直接调用它。这是已知可观测性缺口（登记在 `todo.md` 的 `KB-63`）。

### 6.5 工作流（task LLM）的例外

`OllamaBackend.raw_client` 是一个**刻意的 OpenAI 兼容侧车**（指向 `{native_root}/v1`），
只被 `WorkflowEngine` 的 Planner/Executor 使用（OpenAI SDK 形状）。
⇒ **后台任务路径仍不受 Elysia 的 runtime context 控制**（其窗口由侧车默认值决定）。

### 6.6 `.env.example` 里 Ollama 那一行的读法

```bash
# Ollama 本地 LLM
# OLLAMA_URL=http://localhost:11434/v1
```

这一行写的是**兼容端点**形态。它**不会**改变协议选择（backend 会剥掉 `/v1` 得到 native 根），
但它容易被读成"走 `/v1/chat/completions`"——请以 §6.1 为准。

---

## 7. 健康与就绪

### 7.1 `/health`（liveness）

**恒 HTTP 200**，零网络探测。返回 `status: "ok"` + 运行时信息
（`fsm` / `run_state` / `llm` / `memory.{session_count,long_term_count}` / `tools` /
`version` / `build_sha` / `uptime` / `request_id`）；`fsm`/`run_state` 取不到时降级为 `"unknown"`。

### 7.2 `/ready`（readiness）

**依赖不 ok 时 503**。返回 `status`（`ok`/`degraded`）、`chatReady`、`fullReady`、
运行时信息与 `dependencies{mongo, milvus, embedding, idempotency, identity, lifecycle}`。

每项探针有 2s 超时（超时/异常/非 awaitable 都算 down）：

| 字段 | 判据 |
|---|---|
| `mongo` | 并行探测 session / message / attachment 三个仓储的 `health_check` |
| `identity` | `user_identity_store.is_available is True`（**不是** ping——ping 通但档案不可解析不算 ready） |
| `lifecycle` | `agent.state == RunState.RUNNING`（停机窗口立刻掉出 readiness） |
| `idempotency` | 仓储 ping；产品模式下还要求 `not needs_recovery` |
| `milvus` / `embedding` | 只进 `fullReady`，**不**门控 HTTP——文本可降级运行 |

`chatReady = mongo ∧ identity ∧ (非产品模式 ∨ idempotency) ∧ lifecycle`；
`fullReady = chatReady ∧ milvus ∧ embedding`。

### 7.3 其他运维端点

| 端点 | 鉴权 |
|---|---|
| `GET /ws` | 产品 token（`AGENT_ACCESS_TOKEN`） |
| `GET /`（仅 `SERVE_CHAT=1` 且 `static/index.html` 存在）、`GET /debug`（需 `static/debug.html`） | 无 |
| `POST\|GET /shutdown`、`POST /restart`、`GET /tools`、`POST /tools/reload` | **admin token（`SERVER_AUTH_TOKEN`）** |
| `/api/*`（chat / messages / providers / tts / images / uploads / sessions / attachments / tools / me / characters …） | 产品 token；附件下载走 HMAC 短签 |
| `/debug/api/*`（含 SSE `/debug/api/tasks/stream`） | **admin token** |

**没有 `/metrics` 端点**——Not currently provided by the project.

### 7.4 预检（不是健康检查）

`python app.py --check` → `Module/config/preflight.py::run(config, full=False)`，
只做诊断、从不启动服务；退出码 0/1。11 项（8 项必须 + 3 项 WARN），
`--full` 再加 5 项连通性探活 = 16 项。逐项清单见
[`Module/config/README.md`](../Module/config/README.md)。

---

## 8. 日志

日志目录 = `ProjectPath.LOG.resolve()`（默认 `log/`，可用 `ELYSIA_PATH_LOG` 覆盖），
由 `core/utils/log_setup.py::setup(verbose)` 在 `app.py::main` 里初始化一次。

| 文件 | 级别 | 轮转 | 来源 |
|---|---|---|---|
| `app.log` | INFO+ | 10 MB × 3 | `core` / `Module` / `asyncio` |
| `app-debug.log` | DEBUG+ | 20 MB × 5 | 同上（始终 DEBUG） |
| `conversation.log` | INFO | 10 MB × 3 | 专用 `core.conversation`（`propagate=False`），`>>>`/`<<<` 前缀 |
| `mongo.log` | 库 WARNING+ / 应用 DEBUG | 10 MB × 3 | `pymongo` / `motor` + `core.storage.*_repo`、`core.memory.session`、`core.monitoring.token_tracker` |
| `milvus.log` | 库 WARNING+ / 应用 DEBUG | 10 MB × 3 | `pymilvus` / `milvus_client` + `core.memory.long_term`、`core.storage.memory_repo` |
| `http.log` | WARNING+ | 10 MB × 3 | `httpx` / `httpcore` / `openai` / `urllib3` |
| `embedding.log` | WARNING+ | 10 MB × 3 | `sentence_transformers` / `transformers` / `huggingface_hub` + `core.memory.long_term` |
| `torch.log` | WARNING+ | 10 MB × 3 | `torch` |

**分流按 logger 名**：`core.storage.*` → `mongo.log`、`core.memory.long_term` → `milvus.log`、
其余 → `app.log`；**名字错一层，日志就进错文件**（见
`Module/_common/registry_helpers.py::get_caller_logger`）。每行带
`%(request_id)s`（由 `_RequestIdFilter` 从 ContextVar 注入）。

stdout 保留给对话输出；stderr 是 INFO（`-v` 时 DEBUG）。

**没有日志级别环境变量**——唯一开关是 `--verbose/-v`。Not currently provided by the project.

---

## 9. 备份与恢复边界

### 9.1 本项目**不提供**第一方备份编排

**Project currently has no first-party Mongo/Milvus backup orchestrator.**
证据（逐项核对）：仓库内没有 `*backup*` / `*restore*` / `*dump*` 文件；`Makefile` 没有
backup/dump/snapshot/restore 目标；`app.py` 没有 `backup` 子命令；没有任何 Milvus 快照自动化。
唯一提到 `mongodump` 的地方是**已归档的计划文档**里的一条人工命令，不是随仓库交付的工具。

因此**备份是你的责任**。可用的边界与位置：

| 数据 | 位置 |
|---|---|
| MongoDB | named volume `mongo_data`（production 同） |
| Milvus | `milvus_data` / `milvus_data_prod` |
| etcd（Milvus 元数据） | `etcd_data` |
| MinIO（Milvus 对象存储） | `minio_data` / `minio_data_prod` |
| 附件/上传 | 宿主目录 `data/uploads/**`（**不在 volume 里**） |
| 本地 journal / WAL | 宿主目录 `data/*.jsonl`、`data/*.json` |

> ⚠️ **一致性提示**：Mongo 与 Milvus 是两套独立存储（记忆在 Milvus、会话在 Mongo），
> 分开备份会产生**时间点不一致**的副本。项目没有跨库一致性快照能力——要一致性，
> 请在停机窗口内备份，或接受"记忆与会话可能相差一个窗口"。

### 9.2 项目**确实**提供的恢复/持久化机制

这些不是备份，但它们决定了"崩溃后什么还在"：

| 机制 | 保住什么 | 实现 |
|---|---|---|
| `data/task_result_wal.jsonl` | 后台任务终态（硬杀窗口内）——`write+flush+fsync`，启动 replay 进仓储，**库为权威** | `core/storage/task_result_wal.py`；启动步骤 `task_result_wal_replay` → `task_result_redelivery` |
| `data/consolidator_pending.json` | 未整合的对话缓冲（关机 flush 超时即丢的那部分） | `core/memory/consolidator_pending.py`；启动步骤 `consolidator_catchup` |
| `data/pending_attachment_release.json` | 主操作已提交、附件引用未释放的补偿 | `core/storage/pending_attachment_release.py`；启动步骤 `pending_cleanups` |
| 附件 GC | 启动兜底扫描（软删 >24h 物理删 / active 缺文件软删 / 未关联 >7d 软删 / 磁盘孤儿删 / 隔离区 168h） | `core/storage/attachment_gc.py`；启动步骤 `attachment_gc` |
| `ensure_index` | 新增集合/索引/字段（每次 `connect()` 幂等、只增不改） | `Module/database/documentdb/mongodb.py` |
| `schema_drift` | 只读漂移报告（孤儿索引 warning + `drop_index` 命令；**绝不自动删**） | `core/storage/schema_drift.py`；启动步骤 `schema_drift` |
| 受守卫的唯一索引升级 | `sessions.session_id` 唯一约束（有重复则保持非唯一 + 告警，**不删数据**） | `core/storage/session_repo.py::_ensure_session_id_index` |
| 记忆离线 journal | Milvus 离线期间的待写/待删 | `data/memory_pending_syncs.json`、`data/memory_pending_deletes.json` |
| 会话清理 journal | 会话已删但引用/归档清理失败的重试 | `data/session_pending_cleanup.json` |

### 9.3 数据重置（**不是**备份）

`scripts/clean_db.py` 清理 Mongo 集合 / Milvus 集合 / 运行时数据根（`ProjectPath.DATA`）下的 journal
（`--dry-run` / `--conversation-only` / `--mongo-only` / `--milvus-only` / `--files-only`，
带类型化确认）。它**永不删除** `user_identity_profiles`。

> ⚠️ **必须先停服务**：`idempotency` / `desktop_calls` / `attachments` 是 fail-closed 的，
> 且 `desktop_calls` 有进程内读缓存——服务在跑时重置会被缓存"复活"成脏行。

破坏性 debug 端点（清 LTM / 归档 / 会话）要求 `?confirm=<resource>`，否则 400。

---

## 10. 故障排查

| 症状 | 原因 / 处置 |
|---|---|
| `ValueError: Unknown scheme for proxy URL URL('socks://…')` | 环境里 `ALL_PROXY=socks://…`，httpx 不支持 socks scheme。`env -u ALL_PROXY -u all_proxy <cmd>`，或按 `scripts/start.sh` 的做法清空（HTTP 代理可保留） |
| 长期记忆静默不可用 | 嵌入模型没下全：预检曾用"目录存在且非空"而 loader 要求 `config.json`，导致预检绿而 loader 拒绝。跑 `python scripts/download_model.py`；代理/镜像相关的 `SSL UNEXPECTED_EOF` 见 [`archive/elysia-install-issues.md`](archive/elysia-install-issues.md) |
| 每个 ollama 请求都 `TypeError: '>' not supported between 'str' and 'int'` | `context_window` 被写成带引号的字符串（`"8192"`）。现由 `declared_context_window` 单点解析；若还出现请检查是否有第二条读取路径 |
| provider 突然"连不上" | `.env` 里留了一行空的 `<PROVIDER>_URL=`。现空串按未设置处理；仍失败请看 `http.log` |
| 严格 OpenAI 风格 API 报 400，日志只有 `GENERATION_PROVIDER_UNAVAILABLE` | DeepSeek 风格的顶层 `thinking` 参数被拒。由 `providers.yaml::supports_thinking` 门控；未声明的 provider 会丢弃它并 warning |
| 会话域整体 503，启动却报健康 | 同名索引不同选项（Mongo code 86 `IndexKeySpecsConflict`）：`schema_drift` 的 `option_conflicts` 会报 error。按 [`database-storage.md`](database-storage.md) §五 手动 `drop_index` 后重启 |
| 唯一索引降级后不自我修复 | 设计如此。恢复顺序：清重复 → `drop_index()` → 重启（**不要**期待自动猜 winner） |
| Milvus 端口能连但用例说不可用 | docker-proxy 会提前占住宿主端口。等 healthy（compose healthcheck 用容器内 `curl :9091/healthz`），别只 `nc -z` |
| `Ctrl+C` 后不退出/不清理 | 若用 `conda run` 启动，SIGTERM 不会被转发 ⇒ `Agent.stop()` 的清理步骤不执行。用 `scripts/start.sh`（它 exec env 的 python） |
| 旧 CPU（无 AVX）上 `mongo:8` 无限重启（exit 132） | 把 compose 的 image 换成 `mongo:4.4`（最后支持非 AVX 的版本）；Milvus 异常可回退 `milvusdb/milvus:v2.4.0` |
| 日志跑到错误的文件 | logger 名错了一层（分流按名字）。检查 `get_caller_logger` / `inject_logger` 的用法 |
| `/debug` 或 `/` 404 | 未构建前端（缺 `static/debug.html` / `static/index.html`），或 v1 聊天被停用（需 `SERVE_CHAT=1`） |
| `/debug/api/*` 全部 401 | 只配了 `AGENT_ACCESS_TOKEN`：管理面只认 `SERVER_AUTH_TOKEN`（两个权限域刻意分离） |
| 预检报"required variable … is missing a value" | `.env` 缺数据库密码。跑 `bash scripts/setup.sh` 生成，或按 `.env.example` 手填 |

---

## 11. 版本权威

| 运行时 | **支持版本** | 权威文件 | 其它地方的说法 |
|---|---|---|---|
| Python | **3.12** | `environment.yml`（`python=3.12`）、`scripts/setup.sh`、`pyrightconfig.json`（`pythonVersion: 3.12`） | 代码侧的**兜底分支**（`Module/config/preflight.py::_check_python` 与 `scripts/setup.sh` 的非 conda 分支）接受 `>= 3.11`——那是**下界判据**，不是"受支持并验证过的版本"。**3.12 才是 lock 与 CI 实际构建的版本**（canonical 文档一律写 3.12） |
| Node.js | **22.x（22.23.0，LTS 线）** | `.github/workflows/ci.yml` 的 `frontend` job（显式 pin + 断言主版本，漂移即红） | `scripts/setup.sh` 的前端分支接受 `>= 18`——那是**构建 v1 UI 的下界**（canonical 文档一律写 22.x）。本仓**没有** `.nvmrc` / `engines` 字段，所以本地 Node 不会自动对齐 CI |
| conda env 名 | `Elysia` | `Makefile`（`CONDA_ENV`）、`environment.yml` | — |

> **已知不一致（如实记录，不在文档层掩盖）**：`scripts/setup.sh` 用 `python=3.12` 建环境，
> 却在旁边的兜底分支里断言 `>= 3.11`；预检同理。要收敛到单一判据需要改代码（runtime 语义），
> 不属本文范围。运维结论：**按 3.12 + Node 22.x 准备环境**。

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [`Module/config/README.md`](../Module/config/README.md) | 配置基础设施：加载优先级、env 类型转换、`providers.yaml` 语义（字段级权威） |
| [`.env.example`](../.env.example) | 环境变量模板（含数据库段与各 provider 的 key/URL） |
| [`database-storage.md`](database-storage.md) | 存储设计：集合/索引/TTL/GC/删除语义/漂移自检 |
| [`credential-handling.md`](credential-handling.md) | 凭据处理规范与泄露响应 |
| [`electron-backend-security.md`](electron-backend-security.md) | 远程认证 / TLS / 限流 / 幂等 / 附件签名 / 探针分层 |
| [`ci.md`](ci.md) | CI 布局与信任边界（与本文的 §11 版本权威同源） |
| [`testing.md`](testing.md) | 测试分层与**提交前必跑清单**（含真实 MongoDB 层） |
| [`architecture.md`](architecture.md) | 系统架构（本文只讲运维面） |
